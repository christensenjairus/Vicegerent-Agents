#!/usr/bin/env python3
"""Register shell hooks for the ``dashboard``/``serve`` entrypoints too.

``hermes_cli/main.py`` gates plugin + shell-hook registration behind
``_prepare_agent_startup()``, which returns early unless the subcommand is in
``_AGENT_COMMANDS`` (``None``/``chat``/``acp``/``rl``) or matches an
``_AGENT_SUBCOMMANDS`` entry (``cron run|tick``, ``gateway run``, ``mcp
serve``). ``dashboard`` and its headless twin ``serve`` are in neither, so
``register_from_config()`` never runs and every config-declared ``hooks:``
entry is a silent no-op -- even though both DO run full agent turns
(``serve`` is the desktop app's backend) and fire the hook sites normally.

The failure is invisible: ``hermes hooks doctor`` validates config,
allowlist, and script health, not whether the *running* process registered,
so it reports all-green while nothing fires. This bit us on HAH-133 -- the
shared-skills republish hook worked under the gateway and never fired in a
desktop-app session.

Adding the two commands to ``_AGENT_COMMANDS`` is the minimal fix: it is
exactly the predicate the gate consults. Management commands stay excluded,
so they still skip discovery and consent prompts. Consent is unchanged --
registration still requires ``--accept-hooks``, ``HERMES_ACCEPT_HOOKS=1``,
or ``hooks_auto_accept``.

Fail-loud by design: a missing or duplicated anchor raises and fails the
build. Idempotent. Remove once upstream registers hooks for these
entrypoints.
"""
import importlib.util
import sys

ANCHOR = '_AGENT_COMMANDS = {None, "chat", "acp", "rl"}'
REPLACEMENT = '_AGENT_COMMANDS = {None, "chat", "acp", "rl", "dashboard", "serve"}'


def main() -> int:
    spec = importlib.util.find_spec("hermes_cli.main")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli/main.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if REPLACEMENT in src:
        print(f"patch: dashboard/serve already in _AGENT_COMMANDS in {path} - no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 '{ANCHOR}' in {path}, found {count} "
            "(upstream refactored the _prepare_agent_startup() gate - "
            "re-verify which entrypoints register shell hooks)"
        )
    src = src.replace(ANCHOR, REPLACEMENT)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: registered shell hooks for dashboard/serve in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
