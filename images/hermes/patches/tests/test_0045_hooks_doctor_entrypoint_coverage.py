#!/usr/bin/env python3
"""Behavioral regression test for patch 0045 (``hermes hooks doctor`` must
report which entrypoints actually register shell hooks).

Run inside a Hermes image after the patch is applied, or against a scratch
copy during development:

    HERMES_CLI_HOOKS=/path/to/hermes_cli/hooks.py python3 \\
        test_0045_hooks_doctor_entrypoint_coverage.py

The bug this guards: the doctor validates config, allowlist, mtime drift and
script health, but never whether the running entrypoint registers hooks at
all. A hook can pass every check and still be a silent no-op -- which is
exactly what happened on HAH-133 under ``dashboard``/``serve``.

Asserts the coverage report reads the REAL gate predicate from
hermes_cli.main (so it cannot drift), and that it flags an excluded
entrypoint rather than printing an unqualified all-green.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile

# Drives the patched _print_entrypoint_coverage() with the gate predicate
# forced to a known state, so the assertion is about behaviour, not text.
DRIVER = r'''
import sys, io, types
HOOKS, EXCLUDE = sys.argv[1], sys.argv[2] == "exclude"
sys.path = [p for p in sys.path if not p.startswith("/tmp")]
import hermes_cli
from hermes_cli import main as _main
if EXCLUDE:
    _main._AGENT_COMMANDS = {None, "chat", "acp", "rl"}
else:
    _main._AGENT_COMMANDS = {None, "chat", "acp", "rl", "dashboard", "serve"}
mod = types.ModuleType("hermes_cli.hooks"); mod.__package__ = "hermes_cli"
exec(compile(io.open(HOOKS, encoding="utf-8").read(), HOOKS, "exec"), mod.__dict__)
mod._print_entrypoint_coverage()
'''


def _run(hooks: str, mode: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(DRIVER)
        driver = f.name
    try:
        r = subprocess.run(
            [sys.executable, driver, hooks, mode],
            capture_output=True, text=True, cwd="/",
            env={**os.environ, "PYTHONPATH": ""},
        )
        if r.returncode != 0:
            raise SystemExit(f"FAIL: driver crashed ({mode}):\n{r.stderr[-2000:]}")
        return r.stdout
    finally:
        os.unlink(driver)


def main() -> int:
    hooks = os.environ.get("HERMES_CLI_HOOKS")
    if not hooks:
        import importlib.util
        spec = importlib.util.find_spec("hermes_cli.hooks")
        if spec is None or not spec.origin:
            raise SystemExit("cannot locate hermes_cli/hooks.py; set HERMES_CLI_HOOKS")
        hooks = spec.origin

    src = io.open(hooks, encoding="utf-8").read()
    if "_print_entrypoint_coverage" not in src:
        raise SystemExit(
            f"FAIL: patch 0045 is not applied to {hooks} — `hermes hooks doctor` "
            "will report all-green for hooks that can never fire."
        )

    # 1. With dashboard/serve gated IN, they must be reported as registering
    #    and must NOT be flagged as no-ops.
    out = _run(hooks, "include")
    if "dashboard" not in out or "serve" not in out:
        raise SystemExit(f"FAIL: registering entrypoints not listed:\n{out}")
    if "SILENT NO-OP" in out:
        raise SystemExit(f"FAIL: false no-op warning when hooks DO register:\n{out}")

    # 2. With them gated OUT, the doctor must say so — this is the whole point.
    out = _run(hooks, "exclude")
    if "SILENT NO-OP" not in out:
        raise SystemExit(
            "FAIL: an excluded entrypoint was not flagged. The doctor would "
            f"report all-green while nothing fires:\n{out}"
        )
    for name in ("dashboard", "serve"):
        if name not in out.split("SILENT NO-OP")[0].split("✗")[-1]:
            raise SystemExit(f"FAIL: {name!r} missing from the no-op list:\n{out}")

    print("PASS: doctor reports real entrypoint coverage and flags excluded ones")
    return 0


if __name__ == "__main__":
    sys.exit(main())
