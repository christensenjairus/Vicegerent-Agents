#!/usr/bin/env python3
"""Run the boot-time config migration when the container is already Hermes.

Kubernetes starts the whole image as uid 10000.  The upstream stage-2 hook
nevertheless invokes ``s6-setuidgid hermes`` for the migration; changing the
supplementary group list is forbidden by the pod security context, so s6 exits
before Python runs.  Execute Python directly when the current uid already is
the Hermes uid, retaining the privilege drop for rootful containers.

Fail-loud on upstream drift and idempotent.  Remove once upstream handles an
already-correct uid itself.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


ANCHOR = '''if [ -f "$HERMES_HOME/config.yaml" ]; then
    s6-setuidgid hermes "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/docker_config_migrate.py" \\
        || echo "[stage2] Warning: docker_config_migrate.py failed; continuing"
fi'''

REPLACEMENT = '''if [ -f "$HERMES_HOME/config.yaml" ]; then
    if [ "$(id -u)" = "$actual_hermes_uid" ]; then
        "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/docker_config_migrate.py" \\
            || echo "[stage2] Warning: docker_config_migrate.py failed; continuing"
    else
        s6-setuidgid hermes "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/docker_config_migrate.py" \\
            || echo "[stage2] Warning: docker_config_migrate.py failed; continuing"
    fi
fi'''


def main() -> int:
    path = Path(os.environ.get("HERMES_STAGE2_HOOK", "/opt/hermes/docker/stage2-hook.sh"))
    src = path.read_text(encoding="utf-8")

    if '"$(id -u)" = "$actual_hermes_uid"' in src:
        print("0046: already applied")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch 0046: expected exactly 1 config migration anchor in {path}, found {count}"
        )

    path.write_text(src.replace(ANCHOR, REPLACEMENT), encoding="utf-8")
    print("0046: config migration supports an already-correct runtime uid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
