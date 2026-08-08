#!/usr/bin/env python3
"""Reconcile the repository's single locked Python environment."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"ERROR - Python 3.11+ is required (found {sys.version.split()[0]})")

import fcntl
import tomllib


def uv_requirement(pyproject: Path) -> str:
    dependencies = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    matches = [dependency for dependency in dependencies if dependency.startswith("uv==")]
    if len(matches) != 1:
        raise SystemExit("ERROR - pyproject.toml must contain exactly one uv== dependency")
    return matches[0]


def installed_uv_version(uv: Path) -> str | None:
    if not uv.is_file() or not os.access(uv, os.X_OK):
        return None
    result = subprocess.run([uv, "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip().removeprefix("uv ")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} REPO_ROOT")

    root = Path(sys.argv[1]).resolve()
    pyproject = root / "pyproject.toml"
    lockfile = root / "uv.lock"
    venv = root / ".venv"
    python = venv / "bin" / "python"
    uv = venv / "bin" / "uv"

    if not pyproject.is_file():
        raise SystemExit(f"ERROR - Python project file not found: {pyproject}")
    if not lockfile.is_file():
        raise SystemExit(f"ERROR - Python lock file not found: {lockfile}")

    requirement = uv_requirement(pyproject)
    expected_version = requirement.removeprefix("uv==")

    # A kernel lock has no stale-lock state: it is released whenever the holder exits.
    # Keeping bootstrap and sync under one lock also protects callers that use a mock uv.
    with (root / ".venv.lock").open("a+") as environment_lock:
        fcntl.flock(environment_lock, fcntl.LOCK_EX)

        if installed_uv_version(uv) != expected_version:
            if not python.is_file():
                subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            subprocess.run([python, "-m", "ensurepip", "--upgrade"], check=True, stdout=subprocess.DEVNULL)
            env = os.environ | {"PIP_DISABLE_PIP_VERSION_CHECK": "1"}
            subprocess.run([python, "-m", "pip", "install", "--quiet", requirement], check=True, env=env)

        env = os.environ | {"UV_PROJECT_ENVIRONMENT": str(venv)}
        subprocess.run([uv, "sync", "--project", root, "--locked", "--quiet"], check=True, env=env)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        command = shlex.join(str(argument) for argument in error.cmd)
        raise SystemExit(f"ERROR - Python environment command failed ({error.returncode}): {command}") from None
