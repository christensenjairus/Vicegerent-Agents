#!/usr/bin/env python3
"""Generate the victoria-logs Vector `redactor` VRL block from the canonical
secret-pattern JSON.

The Vector agent's log-scrubbing leg lives in a static upstream-chart values file
(stages/values/victoria-logs.yaml) that Helm consumes with `-f`, and VRL cannot
build a regex from a runtime string — so unlike the egress-proxy scrubber
(helm --set-file) and the shim (//go:embed), this leg cannot derive its patterns
at render time. Instead it keeps a GENERATED copy of the same
images/mcp-cerbos-shim/internal/server/secret-patterns.json, emitted here between
sentinel comments and guarded by `--check` in scripts/validate.sh so it can never
silently drift from the canonical source.

Modes:
  (default) / --write   rewrite the interior of the sentinel block in-place
  --check               exit non-zero (with a diff) if the block is stale
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATTERNS_FILE = REPO_ROOT / "images/mcp-cerbos-shim/internal/server/secret-patterns.json"
TARGET_FILE = REPO_ROOT / "stages/values/victoria-logs.yaml"
BEGIN_MARK = "BEGIN GENERATED redactor"
END_MARK = "END GENERATED redactor"


def statements(patterns_file):
    """One VRL statement per pattern. The first uses `replace!` because `.message`
    is not yet proven to be a string coming out of the parser transform; every
    subsequent call operates on the string the previous one returned, so plain
    (infallible) `replace` is correct — and `!` on an infallible call is a VRL
    compile error, so the split is load-bearing, not stylistic."""
    defs = json.loads(Path(patterns_file).read_text())
    if not defs:
        sys.exit("secret-patterns.json decoded to an empty list — refusing to generate an empty redactor")
    out = []
    for i, d in enumerate(defs):
        fn = "replace!" if i == 0 else "replace"
        out.append(f".message = {fn}(.message, r'{d['regex']}', \"<masked>\", count: -1)")
    return out


def find_block(lines, target):
    begin = end = None
    for i, ln in enumerate(lines):
        if BEGIN_MARK in ln:
            begin = i
        elif END_MARK in ln:
            end = i
            break
    if begin is None or end is None or end <= begin:
        sys.exit(f"sentinels ({BEGIN_MARK!r} / {END_MARK!r}) not found in {target}")
    indent = lines[begin][: len(lines[begin]) - len(lines[begin].lstrip())]
    return begin, end, indent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify the block is up to date; do not write")
    ap.add_argument("--patterns", default=str(PATTERNS_FILE))
    ap.add_argument("--target", default=str(TARGET_FILE))
    args = ap.parse_args()

    target = Path(args.target)
    lines = target.read_text().splitlines(keepends=True)
    begin, end, indent = find_block(lines, args.target)
    expected = [f"{indent}{s}\n" for s in statements(args.patterns)]
    current = lines[begin + 1 : end]

    if args.check:
        if current != expected:
            sys.stderr.write(
                f"ERROR - {args.target} redactor block is stale vs {args.patterns}.\n"
                f"        Run: python3 scripts/gen-vector-redactor.py\n\n"
            )
            sys.stderr.writelines(
                difflib.unified_diff(current, expected, fromfile="committed", tofile="generated")
            )
            sys.exit(1)
        print(f"OK - {args.target} redactor block matches {args.patterns}")
        return

    if current != expected:
        lines[begin + 1 : end] = expected
        target.write_text("".join(lines))
        print(f"WROTE - regenerated {len(expected)} redactor statements in {args.target}")
    else:
        print(f"OK - {args.target} already up to date")


if __name__ == "__main__":
    main()
