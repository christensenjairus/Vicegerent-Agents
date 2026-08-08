#!/usr/bin/env python3
"""Behavioral regression test for patch 0044 (shell hooks for the
``dashboard``/``serve`` entrypoints).

Run inside the agent image after the Hermes patch is applied, or against a scratch
copy during development:

    HERMES_CLI_MAIN=/path/to/hermes_cli/main.py python3 \
        test_0044_shell_hooks_dashboard_serve.py

Checks the gate predicate via ``ast`` (not a string match, so an upstream
refactor fails loudly), then registers and fires a real hook end-to-end.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MUST_PASS = {None, "chat", "acp", "rl", "dashboard", "serve"}
# Must stay excluded: the gate exists so these skip discovery and consent.
MUST_NOT_PASS = {"config", "doctor", "tools", "version", "cron", "gateway", "mcp"}


def _gate_sets(path: str) -> dict:
    """Read the two module-level sets the gate consults, via ast."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in (
                "_AGENT_COMMANDS",
                "_AGENT_SUBCOMMANDS",
            ):
                found[target.id] = ast.literal_eval(node.value)
    missing = {"_AGENT_COMMANDS", "_AGENT_SUBCOMMANDS"} - set(found)
    if missing:
        raise AssertionError(
            f"could not find {sorted(missing)} in {path} — upstream refactored "
            "the _prepare_agent_startup() gate; re-verify patch 0044"
        )
    return found


def check_gate(path: str) -> None:
    sets = _gate_sets(path)
    agent_commands = sets["_AGENT_COMMANDS"]
    subcommands = sets["_AGENT_SUBCOMMANDS"]

    for cmd in MUST_PASS:
        assert cmd in agent_commands, (
            f"{cmd!r} does not pass the _prepare_agent_startup() gate — shell "
            f"hooks will silently never register for it. _AGENT_COMMANDS="
            f"{sorted(str(c) for c in agent_commands)}"
        )

    for cmd in MUST_NOT_PASS:
        assert cmd not in agent_commands, (
            f"{cmd!r} unexpectedly passes the bare-command gate — patch 0044 "
            "must not widen it beyond dashboard/serve (management commands "
            "would pay plugin discovery and trigger hook-consent prompts)"
        )

    # cron/gateway/mcp opt in via sub-attr; the patch must not disturb that.
    assert subcommands.get("gateway") == ("gateway_command", {"run"}), (
        f"gateway sub-gate changed: {subcommands.get('gateway')!r}"
    )
    assert subcommands.get("cron") == ("cron_command", {"run", "tick"}), (
        f"cron sub-gate changed: {subcommands.get('cron')!r}"
    )
    print(
        "  ok  gate: dashboard/serve pass; management commands still excluded; "
        "sub-gates unchanged"
    )


def check_hook_dispatch() -> None:
    """Register and fire a real shell hook the way the runtime does."""
    if "/opt/hermes" not in sys.path:
        sys.path.insert(0, "/opt/hermes")
    try:
        from agent.shell_hooks import register_from_config
        from hermes_cli.plugins import has_hook, invoke_hook
    except ImportError as exc:  # pragma: no cover - env guard
        raise AssertionError(
            f"failed to import Hermes hook machinery — run this inside the "
            f"agent image with /opt/hermes importable: {exc}"
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        sentinel = tmpdir / "fired.txt"
        script = tmpdir / "hook.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            f"echo fired > {sentinel}\n"
            # Hook wire protocol parses stdout as JSON; anything else warns.
            "echo '{}'\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

        config = {
            "hooks_auto_accept": True,
            "hooks": {
                "post_tool_call": [
                    {
                        "matcher": "skill_manage",
                        "command": str(script),
                        "timeout": 30,
                    }
                ]
            },
        }

        # accept_hooks=False: hooks_auto_accept is the only headless channel.
        specs = register_from_config(config, accept_hooks=False)
        assert specs, (
            "register_from_config() registered nothing with hooks_auto_accept "
            "set and accept_hooks=False — headless consent path is broken"
        )
        assert has_hook("post_tool_call"), (
            "post_tool_call has no registered listener after registration"
        )

        result = invoke_hook(
            "post_tool_call",
            tool_name="skill_manage",
            args={"action": "create"},
            result="ok",
        )
        if hasattr(result, "__await__"):  # pragma: no cover - sync in practice
            import asyncio

            asyncio.run(result)

        assert sentinel.exists(), (
            "the matched post_tool_call hook did not execute — invoke_hook "
            "dispatched nothing to the registered shell script"
        )

        # A non-matching tool name must NOT fire it (matcher is a fullmatch).
        sentinel.unlink()
        result = invoke_hook(
            "post_tool_call",
            tool_name="terminal",
            args={"command": "true"},
            result="ok",
        )
        if hasattr(result, "__await__"):  # pragma: no cover
            import asyncio

            asyncio.run(result)
        assert not sentinel.exists(), (
            "the hook fired for tool_name='terminal' despite a "
            "matcher of 'skill_manage' — matcher is not being honored"
        )

    print("  ok  dispatch: hook registers headless, fires on match, ignores non-match")


def check_sync_script_contract() -> None:
    """The hook's script must emit JSON on stdout, not a log line, or the
    post_tool_call wire protocol logs a warning."""
    path = os.environ.get(
        "VICEGERENT_SYNC_SCRIPT", "/reload/shared-skills/sync-shared-skills.sh"
    )
    if not Path(path).exists():
        print(f"  skip sync-script contract: {path} not mounted here")
        return
    proc = subprocess.run(
        ["bash", path], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{path} exited {proc.returncode}: {proc.stderr}"
    try:
        json.loads(proc.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{path} stdout is not valid JSON ({exc}) — the post_tool_call "
            f"wire protocol will log a warning on every skill_manage call. "
            f"stdout was: {proc.stdout!r}"
        )
    print("  ok  sync script emits valid JSON on stdout")


def main() -> int:
    path = os.environ.get("HERMES_CLI_MAIN", "/opt/hermes/hermes_cli/main.py")
    assert Path(path).exists(), f"no such file: {path}"
    print(f"testing patch 0044 against {path}")
    check_gate(path)
    check_hook_dispatch()
    check_sync_script_contract()
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
