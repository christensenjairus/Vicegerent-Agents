#!/usr/bin/env python3
"""Regression tests for image-tag validation in merge trains."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("validate-image-tags.py")


class MergeTrainImageTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self.env = os.environ.copy()
        self.env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        (self.repo / "scripts").mkdir()
        (self.repo / "images" / "example").mkdir(parents=True)
        (self.repo / "deploy").mkdir()
        shutil.copy2(SCRIPT, self.repo / "scripts" / SCRIPT.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.write_tag("v1.0.19")
        (self.repo / "images" / "example" / "a.txt").write_text("base-a\n")
        (self.repo / "images" / "example" / "b.txt").write_text("base-b\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.rev_parse("HEAD")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            check=check,
            env=self.env,
            text=True,
        )

    def rev_parse(self, ref: str) -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def write_tag(self, tag: str) -> None:
        (self.repo / "images" / "example" / "Makefile").write_text(
            f"IMAGE := registry.example/test/example\nTAG := {tag}\n"
        )
        (self.repo / "deploy" / "example.yaml").write_text(
            f"image: registry.example/test/example:{tag}\n"
        )

    def make_mr(
        self,
        name: str,
        tag: str,
        changed_file: str,
        content: str,
        start_ref: str | None = None,
    ) -> str:
        self.git("switch", "--create", name, start_ref or self.base)
        self.write_tag(tag)
        (self.repo / "images" / "example" / changed_file).write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", name)
        return self.rev_parse("HEAD")

    def make_train_commit(self, first_mr: str, second_mr: str) -> str:
        self.git("switch", "--create", "train", self.base)
        self.git("merge", "--no-ff", "--no-edit", first_mr)
        merge = self.git("merge", "--no-ff", "--no-edit", second_mr, check=False)
        self.assertEqual(merge.returncode, 0, merge.stderr)
        self.assertEqual(len(self.git("show", "-s", "--format=%P", "HEAD").stdout.split()), 2)
        return self.rev_parse("HEAD")

    def validate_train(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/validate-image-tags.py", "--train-since"],
            cwd=self.repo,
            capture_output=True,
            env=self.env,
            text=True,
        )

    def test_rejects_tag_reused_by_an_earlier_train_car(self) -> None:
        self.make_mr("mr-a", "v1.0.20", "a.txt", "change-a\n")
        self.make_mr("mr-b", "v1.0.20", "b.txt", "change-b\n")
        self.make_train_commit("mr-a", "mr-b")

        result = self.validate_train()

        self.assertEqual(result.returncode, 1)
        self.assertIn("TAG is still v1.0.20", result.stderr)
        self.assertIn("rebase it after the earlier train car merges", result.stderr)

    def test_accepts_a_tag_newer_than_the_earlier_train_car(self) -> None:
        self.make_mr("mr-a", "v1.0.20", "a.txt", "change-a\n")
        self.make_mr("mr-b", "v1.0.21", "b.txt", "change-b\n", start_ref="mr-a")
        self.make_train_commit("mr-a", "mr-b")

        result = self.validate_train()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bumped since", result.stdout)

    def test_rejects_a_non_merge_train_checkout(self) -> None:
        self.git("switch", "main")

        result = self.validate_train()

        self.assertEqual(result.returncode, 1)
        self.assertIn("two-parent merge-train commit", result.stderr)


if __name__ == "__main__":
    unittest.main()
