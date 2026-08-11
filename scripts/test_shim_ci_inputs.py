#!/usr/bin/env python3
"""Ensure the pre-commit job runs the shim integration checks."""
from __future__ import annotations

import pathlib
import unittest

import yaml


REPO = pathlib.Path(__file__).resolve().parents[1]


class ShimCiInputTests(unittest.TestCase):
    def test_pre_commit_job_runs_shim_checks(self) -> None:
        pipeline = yaml.safe_load((REPO / ".gitlab-ci.yml").read_text(encoding="utf-8"))
        job = pipeline["validate:pre-commit"]
        self.assertEqual(job["variables"]["HELM_VERSION"], "v4.2.3")
        before_script = job["before_script"]
        self.assertTrue(any("get.helm.sh" in command and "HELM_VERSION" in command for command in before_script), before_script)
        self.assertTrue(any("/usr/local/bin/helm" in command for command in before_script), before_script)
        script = "\n".join(job["script"])
        self.assertIn("images/mcp-cerbos-shim", script)


if __name__ == "__main__":
    unittest.main()
