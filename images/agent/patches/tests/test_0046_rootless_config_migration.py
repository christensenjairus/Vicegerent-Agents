#!/usr/bin/env python3
"""Regression test for rootless boot-time config migration patch 0046."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ANCHOR = '''if [ -f "$HERMES_HOME/config.yaml" ]; then
    s6-setuidgid hermes "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/docker_config_migrate.py" \\
        || echo "[stage2] Warning: docker_config_migrate.py failed; continuing"
fi'''


def main() -> int:
    patch = Path(__file__).resolve().parents[1] / "0046-rootless-config-migration.py"
    with tempfile.TemporaryDirectory() as tmp:
        hook = Path(tmp) / "stage2-hook.sh"
        hook.write_text(ANCHOR + "\n", encoding="utf-8")
        env = {**os.environ, "HERMES_STAGE2_HOOK": str(hook)}

        first = subprocess.run([sys.executable, str(patch)], env=env, text=True, capture_output=True)
        if first.returncode != 0:
            raise SystemExit(f"FAIL: patch failed:\n{first.stderr}")
        second = subprocess.run([sys.executable, str(patch)], env=env, text=True, capture_output=True)
        if second.returncode != 0 or "already applied" not in second.stdout:
            raise SystemExit("FAIL: patch is not idempotent")

        src = hook.read_text(encoding="utf-8")
        direct = '"$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/docker_config_migrate.py"'
        dropped = 's6-setuidgid hermes "$INSTALL_DIR/.venv/bin/python"'
        if '"$(id -u)" = "$actual_hermes_uid"' not in src or direct not in src:
            raise SystemExit("FAIL: already-Hermes uid does not run Python directly")
        if dropped not in src:
            raise SystemExit("FAIL: rootful privilege-drop path was removed")

        syntax = subprocess.run(["bash", "-n", str(hook)], text=True, capture_output=True)
        if syntax.returncode != 0:
            raise SystemExit(f"FAIL: patched hook is invalid shell:\n{syntax.stderr}")

    print("PASS: rootless migration runs directly and rootful migration still drops uid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
