#!/usr/bin/env python3
"""Reject Markdown paragraphs, list items, and blockquotes split across lines."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
LIST_ITEM_RE = re.compile(r"^( *)(?:[-+*]|\d+[.)])\s+")
HORIZONTAL_RULE_RE = re.compile(r"^ {0,3}(?:[-*_]\s*){3,}$")
TABLE_SEPARATOR_RE = re.compile(
    r"^ {0,3}\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$"
)
BLOCKQUOTE_RE = re.compile(r"^ {0,3}> ?")


def split_blockquote(line: str) -> tuple[int, str]:
    """Return a blockquote nesting depth and the unquoted line content."""
    depth = 0
    while match := BLOCKQUOTE_RE.match(line):
        depth += 1
        line = line[match.end() :]
    return depth, line


def is_pipe_row(line: str) -> bool:
    return "|" in line and bool(line.strip())


def is_fence_close(line: str, marker: str) -> bool:
    return bool(
        re.match(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$", line)
    )


def find_violations(lines: Iterable[str]) -> Iterable[int]:
    """Yield one-based line numbers that continue a soft-wrapped Markdown block."""
    fence_marker: str | None = None
    previous_kind: str | None = None
    previous_quote_depth = 0
    previous_list_content_indent = 0
    previous_was_pipe_row = False
    in_table = False

    for number, raw_line in enumerate(lines, start=1):
        quote_depth, line = split_blockquote(raw_line)
        is_lazy_blockquote_continuation = (
            quote_depth < previous_quote_depth and previous_kind == "prose"
        )
        if quote_depth != previous_quote_depth and not is_lazy_blockquote_continuation:
            previous_kind = None
            previous_was_pipe_row = False
            in_table = False

        if fence_marker:
            if is_fence_close(line, fence_marker):
                fence_marker = None
            previous_kind = None
            previous_was_pipe_row = False
            in_table = False
            previous_quote_depth = quote_depth
            continue

        if match := FENCE_OPEN_RE.match(line):
            fence_marker = match.group(1)
            previous_kind = None
            previous_was_pipe_row = False
            in_table = False
            previous_quote_depth = quote_depth
            continue

        if not line.strip():
            previous_kind = None
            previous_was_pipe_row = False
            in_table = False
            previous_quote_depth = quote_depth
            continue

        if in_table and is_pipe_row(line):
            previous_kind = None
            previous_was_pipe_row = True
            previous_quote_depth = quote_depth
            continue
        if in_table:
            in_table = False

        if TABLE_SEPARATOR_RE.match(line) and previous_was_pipe_row:
            in_table = True
            previous_kind = None
            previous_was_pipe_row = True
            previous_quote_depth = quote_depth
            continue

        list_match = LIST_ITEM_RE.match(line)
        indentation = len(line) - len(line.lstrip(" "))
        is_indented_code = (
            indentation >= 4
            and not list_match
            and not (
                previous_kind == "list"
                and indentation < previous_list_content_indent + 4
            )
        )
        is_structural = bool(
            HEADING_RE.match(line)
            or HORIZONTAL_RULE_RE.match(line)
            or line.lstrip().startswith(("|", "<"))
            or is_indented_code
        )
        kind = "list" if list_match else "structural" if is_structural else "prose"

        if previous_kind in {"prose", "list"} and kind == "prose":
            yield number

        previous_kind = kind
        previous_quote_depth = quote_depth
        previous_was_pipe_row = is_pipe_row(line)
        if list_match:
            previous_list_content_indent = list_match.end()


def check(path: Path) -> list[int]:
    return list(find_violations(path.read_text(encoding="utf-8").splitlines()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", metavar="FILE", nargs="+", type=Path)
    args = parser.parse_args()

    failed = False
    for path in args.files:
        violations = check(path)
        for line in violations:
            print(
                f"{path}:{line}: Markdown content must use one physical line per paragraph, list item, or blockquote",
                file=sys.stderr,
            )
        failed |= bool(violations)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
