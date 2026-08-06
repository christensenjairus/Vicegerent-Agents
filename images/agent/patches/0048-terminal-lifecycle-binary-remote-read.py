#!/usr/bin/env python3
"""Keep lifecycle-script scanning safe for binary and NUL-bearing input.

Upstream skips every local file containing a NUL, then asks the terminal backend
to read it again as though it were missing. An executable is decoded and
recursively tokenized until a NUL-bearing path reaches ``os.open``. Distinguish
known executable formats from text: skip the former, normalize NULs in the
latter so protected lifecycle commands remain visible to the scanner.
"""
from __future__ import annotations

import os
from pathlib import Path

MARKER = "vicegerent-patch-0048"


def replace_once(source: str, old: str, new: str, path: Path) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"patch 0048: expected exactly 1 anchor in {path}, found {count}")
    return source.replace(old, new)


def main() -> int:
    root = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
    path = root / "cron" / "lifecycle_guard.py"
    source = path.read_text(encoding="utf-8")

    if MARKER in source:
        print("0048: already applied")
        return 0

    source = replace_once(
        source,
        '''def _read_referenced_script(path: Path) -> tuple[Optional[str], bool]:
''',
        '''def _normalize_referenced_script_content(data: bytes | str) -> str:
    """Return scannable text, or an empty sentinel for a known executable."""
    if isinstance(data, bytes):
        # vicegerent-patch-0048: skip corroborated executable formats, not
        # every NUL-bearing file. Bash discards NULs in comments, here-docs,
        # and even command tokens, so otherwise-textual content must still be
        # scanned after deleting NULs to mirror the shell's lexer.
        is_elf = data.startswith(b"\\x7fELF") and b"\\x00" in data
        is_macho = data.startswith((
            b"\\xfe\\xed\\xfa\\xce", b"\\xce\\xfa\\xed\\xfe",
            b"\\xfe\\xed\\xfa\\xcf", b"\\xcf\\xfa\\xed\\xfe",
            b"\\xca\\xfe\\xba\\xbe", b"\\xbe\\xba\\xfe\\xca",
        )) and b"\\x00" in data
        is_pe = False
        if data.startswith(b"MZ") and len(data) >= 64:
            pe_offset = int.from_bytes(data[60:64], "little")
            is_pe = pe_offset + 4 <= len(data) and data[pe_offset:pe_offset + 4] == b"PE\\x00\\x00"
        if is_elf or is_macho or is_pe:
            return ""
        data = data.replace(b"\\x00", b"").decode("utf-8", errors="replace")
    else:
        # Remote backends return decoded text. ELF is the one signature whose
        # ASCII prefix survives lossy UTF-8 decoding reliably; other binaries
        # are normalized and scanned fail-closed rather than blindly skipped.
        if data.startswith("\\x7fELF") and "\\x00" in data:
            return ""
    return data.replace("\\x00", "")


def _read_referenced_script(path: Path) -> tuple[Optional[str], bool]:
''',
        path,
    )
    source = replace_once(
        source,
        '''    # A NUL byte in the first chunk means this is a binary (ELF/Mach-O/
    # PE), not a shell script — scanning its decoded contents would
    # tokenize machine code and feed junk paths into the recursion
    # (including a `ValueError: embedded null byte` from Path.resolve,
    # #76762). Treat it as "nothing to scan" rather than unsafe: a binary
    # executed by the user is not a referenced *shell script*.
    if b"\\x00" in data:
        return None, False
    if len(data) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return data.decode("utf-8", errors="replace"), False
''',
        '''    normalized = _normalize_referenced_script_content(data)
    if not normalized:
        return "", False
    if len(data) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return normalized, False
''',
        path,
    )
    source = replace_once(
        source,
        '''        if script_text is None and read_remote_script is not None:
            # Local path missing; try the remote backend if one is available.
            script_text = read_remote_script(str(script_path))
        if not script_text:
''',
        '''        if script_text is None and read_remote_script is not None:
            # Local path missing; try the remote backend if one is available.
            script_text = read_remote_script(str(script_path))
            if script_text:
                script_text = _normalize_referenced_script_content(script_text)
        if not script_text:
''',
        path,
    )

    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print("0048: lifecycle guard safely scans local and remote NUL-bearing content")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
