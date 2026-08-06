#!/usr/bin/env python3
"""Vicegerent patch: refresh stale shell-hook approval metadata when the
operator explicitly enables automatic hook acceptance.

Hermes stores each approved script's mtime in shell-hooks-allowlist.json and
``hermes hooks doctor`` flags a script whose current mtime differs. The
platform's hook scripts are baked into the image while the allowlist persists
on /opt/data, so every legitimate script update leaves Doctor red forever:
registration sees the existing event/command pair and never records the new
mtime. The hooks still run, but the diagnostic correctly cannot distinguish a
reviewed image update from unexpected post-approval modification.

The ``--accept-hooks`` flag, ``HERMES_ACCEPT_HOOKS=1``, and
``hooks_auto_accept: true`` are already explicit instructions to approve
configured hooks without a prompt. When any acceptance channel is active and
an existing approval's script mtime differs, record the approval again before
registration. A manually approved hook still reports drift when none of those
channels is active.

Fail-loud if the registration anchor drifts. Idempotent; remove once upstream
refreshes stale approval metadata under explicit auto-accept.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


MARKER = "Vicegerent patch 0049"
ANCHOR = """            already_allowlisted = _is_allowlisted(spec.event, spec.command)

        if not already_allowlisted:
"""
REPLACEMENT = f"""            already_allowlisted = _is_allowlisted(spec.event, spec.command)

        # {MARKER}: explicit auto-accept also approves the current script bytes.
        if already_allowlisted and effective_accept:
            entry = allowlist_entry_for(spec.event, spec.command)
            current_mtime = script_mtime_iso(spec.command)
            if (
                entry
                and current_mtime
                and entry.get("script_mtime_at_approval") != current_mtime
            ):
                _record_approval(spec.event, spec.command)
                logger.info(
                    "shell hook approval refreshed after script mtime changed: %s -> %s",
                    spec.event, spec.command,
                )

        if not already_allowlisted:
"""


def main() -> int:
    root = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
    path = root / "agent" / "shell_hooks.py"
    source = path.read_text(encoding="utf-8")

    if MARKER in source:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = source.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 shell-hook registration anchor in {path}, "
            f"found {count} (upstream drifted — re-verify)"
        )

    patched = source.replace(ANCHOR, REPLACEMENT, 1)
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8")
    print(f"patch: auto-accepted hooks refresh stale approval metadata in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
