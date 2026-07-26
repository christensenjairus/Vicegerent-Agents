#!/usr/bin/env python3
"""Make ``hermes hooks doctor`` report which entrypoints actually register hooks.

``hermes hooks doctor`` validates config, allowlist, mtime drift, and script
health -- everything EXCEPT whether the process that runs the hook will ever
register it. Registration is gated in ``hermes_cli/main.py`` by
``_prepare_agent_startup()``, which returns early unless the subcommand is in
``_AGENT_COMMANDS`` or matches an ``_AGENT_SUBCOMMANDS`` entry. A hook that is
executable, allowlisted, unmodified, and returns clean JSON still never fires
under an excluded entrypoint -- and the doctor prints "All shell hooks look
healthy."

That false green is what made HAH-133 expensive to diagnose: patch 0042 fixed
``dashboard``/``serve`` registration, but the diagnostic that should have
caught it kept reporting success. Fixing the symptom without fixing the
detector leaves the next excluded entrypoint just as silent.

This appends an entrypoint-coverage section to the doctor's output, listing
the commands that DO register hooks and the notable agent-running commands
that do NOT. It reads ``_AGENT_COMMANDS``/``_AGENT_SUBCOMMANDS`` from
``hermes_cli.main`` at runtime rather than hardcoding a copy, so it cannot
drift from the predicate it documents -- if upstream adds an entrypoint, the
doctor reflects it on the next run with no patch change.

Advisory only: it does not increment the problem count, since an excluded
entrypoint is not a misconfiguration unless the operator is relying on it.
Fail-loud by design: a missing or duplicated anchor raises and fails the
build. Idempotent. Remove once upstream's doctor checks registration itself.
"""
import importlib.util
import sys

ANCHOR = '''    if problems:
        print(f"{problems} issue(s) found.  Fix before relying on these hooks.")
    else:
        print("All shell hooks look healthy.")'''

REPLACEMENT = '''    _print_entrypoint_coverage()

    if problems:
        print(f"{problems} issue(s) found.  Fix before relying on these hooks.")
    else:
        print("All shell hooks look healthy under the entrypoints listed above.")


def _print_entrypoint_coverage() -> None:
    """Report which entrypoints register shell hooks.

    Reads the real gate predicate from hermes_cli.main so this can never
    drift from the behaviour it describes.
    """
    try:
        from hermes_cli import main as _main
        agent_commands = set(getattr(_main, "_AGENT_COMMANDS", set()))
        agent_subcommands = dict(getattr(_main, "_AGENT_SUBCOMMANDS", {}))
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ! could not determine entrypoint coverage: {exc}\\n")
        return

    registers = sorted(c for c in agent_commands if c) or []
    for parent, (_attr, subs) in sorted(agent_subcommands.items()):
        registers.extend(f"{parent} {s}" for s in sorted(subs))

    print("Entrypoint coverage (hooks only fire under these):")
    print(f"      ✓ {', '.join(registers)}")
    if None in agent_commands:
        print("        (bare `hermes` with no subcommand also registers)")

    # Commands that run agent turns but are NOT gated in -- the silent-failure
    # class this check exists to surface.
    notable = ["dashboard", "serve", "cron run", "cron tick", "gateway run", "mcp serve"]
    excluded = [
        c for c in notable
        if c not in agent_commands and c not in registers
    ]
    if excluded:
        print(f"      ✗ {', '.join(excluded)} — configured hooks are a SILENT NO-OP here")
        print("        (a hook can pass every check above and still never fire)")
    print()'''


def main() -> int:
    spec = importlib.util.find_spec("hermes_cli.hooks")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli/hooks.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if "_print_entrypoint_coverage" in src:
        print("0043: already applied")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch 0043: expected exactly 1 occurrence of the doctor summary "
            f"anchor in {path}, found {count}"
        )

    src = src.replace(ANCHOR, REPLACEMENT)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    print("0043: hooks doctor now reports entrypoint coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
