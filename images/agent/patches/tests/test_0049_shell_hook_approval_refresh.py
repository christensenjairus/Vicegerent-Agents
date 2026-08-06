#!/usr/bin/env python3
"""Regression test for auto-accepted shell-hook approval refresh patch 0049."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DRIVER = r'''
import os
from pathlib import Path
import sys

home = Path(sys.argv[1])
accept_mode = sys.argv[2]
os.environ["HERMES_HOME"] = str(home)
if accept_mode == "env":
    os.environ["HERMES_ACCEPT_HOOKS"] = "1"

from agent import shell_hooks

home.mkdir(parents=True)
script = home / "probe.sh"
script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
script.chmod(0o755)
os.utime(script, (1_700_000_000, 1_700_000_000))

command = str(script)
shell_hooks._record_approval("post_tool_call", command)
before = shell_hooks.allowlist_entry_for("post_tool_call", command)
assert before is not None

os.utime(script, (1_700_000_100, 1_700_000_100))
current_mtime = shell_hooks.script_mtime_iso(command)
assert current_mtime != before["script_mtime_at_approval"]

config = {
    "hooks_auto_accept": accept_mode == "config",
    "hooks": {
        "post_tool_call": [
            {"matcher": "skill_manage", "command": command, "timeout": 5}
        ]
    },
}
registered = shell_hooks.register_from_config(
    config, accept_hooks=accept_mode == "argument"
)
assert registered, "the allowlisted probe hook was not registered"
after = shell_hooks.allowlist_entry_for("post_tool_call", command)
assert after is not None

if accept_mode != "none":
    assert after["script_mtime_at_approval"] == current_mtime, (
        "automatic acceptance did not refresh the stale script approval"
    )
else:
    assert after == before, "manual approval was refreshed without explicit auto-accept"
'''


def run_driver(root: Path, home: Path, accept_mode: str) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": f"{root}:/opt/hermes",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", DRIVER, str(home), accept_mode],
        cwd="/",
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise SystemExit(
            f"FAIL: accept_mode={accept_mode} approval probe failed\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-fix", action="store_true")
    args = parser.parse_args()

    source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
    installed = source_root / "agent" / "shell_hooks.py"
    live_before = installed.read_text(encoding="utf-8")
    patch = Path(__file__).resolve().parents[1] / "0049-shell-hook-approval-refresh.py"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        shutil.copytree(source_root / "agent", root / "agent")
        env = {
            **os.environ,
            "HERMES_ROOT": str(root),
            "PYTHONPATH": f"{root}:/opt/hermes",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        if not args.pre_fix:
            first = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if first.returncode:
                raise SystemExit(f"FAIL: patch failed:\n{first.stderr}")
            second = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if second.returncode or "already applied" not in second.stdout:
                raise SystemExit("FAIL: patch is not idempotent")

        for accept_mode in ("none", "config", "env", "argument"):
            run_driver(root, Path(tmp) / f"{accept_mode}-home", accept_mode)

    if installed.read_text(encoding="utf-8") != live_before:
        raise SystemExit("FAIL: test mutated the installed Hermes tree")
    print("PASS: explicit hook auto-accept refreshes stale approval metadata only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
