#!/usr/bin/env python3
"""Regression tests for the Markdown soft-wrap check."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-markdown-soft-wrap.py")
SPEC = importlib.util.spec_from_file_location("check_markdown_soft_wrap", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MarkdownSoftWrapTests(unittest.TestCase):
    def violations(self, text: str) -> list[int]:
        return list(MODULE.find_violations(text.splitlines()))

    def test_rejects_a_wrapped_paragraph(self) -> None:
        self.assertEqual(self.violations("First line\nsecond line\n"), [2])

    def test_rejects_a_wrapped_list_item(self) -> None:
        self.assertEqual(self.violations("- First line\n  second line\n"), [2])

    def test_allows_structural_markdown_and_fenced_code(self) -> None:
        text = """# Heading

A complete paragraph.

| Name | Value |
|---|---|
| one | two |

```text
first line
second line
```

> A complete blockquote.
"""
        self.assertEqual(self.violations(text), [])

    def test_rejects_a_wrapped_blockquote(self) -> None:
        self.assertEqual(self.violations("> First line\n> second line\n"), [2])

    def test_rejects_a_lazy_blockquote_continuation(self) -> None:
        self.assertEqual(self.violations("> First line\nlazy continuation\n"), [2])

    def test_rejects_a_nested_lazy_blockquote_continuation(self) -> None:
        self.assertEqual(self.violations("> > First line\n> lazy continuation\n"), [2])

    def test_allows_structural_blocks_inside_a_blockquote(self) -> None:
        text = "> - one\n> - two\n>\n> | Name | Value |\n> |---|---|\n> | one | two |\n"
        self.assertEqual(self.violations(text), [])

    def test_tracks_matching_fence_delimiter_before_resuming_prose(self) -> None:
        text = "````text\n```\nalpha\nbeta\n````\nFirst line\nsecond line\n"
        self.assertEqual(self.violations(text), [7])

    def test_allows_pipe_tables_without_outer_pipes(self) -> None:
        text = "Name | Value\n--- | ---\none | two\ntwo | three\n"
        self.assertEqual(self.violations(text), [])

    def test_rejects_a_nested_list_item_continuation(self) -> None:
        text = "- parent\n    - nested first\n      nested continuation\n"
        self.assertEqual(self.violations(text), [3])


if __name__ == "__main__":
    unittest.main()
