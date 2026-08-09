#!/usr/bin/env python3
"""Ensure the shim integration job watches its deployed contract inputs."""
from __future__ import annotations

import pathlib
import unittest

import yaml


REPO = pathlib.Path(__file__).resolve().parents[1]


class ShimCiInputTests(unittest.TestCase):
    def test_shim_job_watches_required_deployed_contract_inputs(self) -> None:
        pipeline = yaml.safe_load((REPO / ".gitlab-ci.yml").read_text(encoding="utf-8"))
        rules = pipeline["validate:shim-go-test"]["rules"]
        changes = next(rule["changes"] for rule in rules if "changes" in rule)
        self.assertTrue(
            {
                "images/mcp-cerbos-shim/**/*",
                "host/mcp/toolhive-servers.json",
                "charts/mcp-cerbos-shim/**/*",
                "values.defaults.yaml",
                "values.example.yaml",
            }.issubset(changes),
            changes,
        )
        self.assertNotIn("charts/cerbos-policies/policies/**/*", changes)
        job = pipeline["validate:shim-go-test"]
        self.assertEqual(job["variables"]["HELM_VERSION"], "v4.2.3")
        before_script = job["before_script"]
        self.assertTrue(any("get.helm.sh" in command and "HELM_VERSION" in command for command in before_script), before_script)
        self.assertTrue(any("/usr/local/bin/helm" in command for command in before_script), before_script)


if __name__ == "__main__":
    unittest.main()
