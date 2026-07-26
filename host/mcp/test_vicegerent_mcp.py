#!/usr/bin/env python3
"""Focused regression tests for interactive Vicegerent MCP CLI helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("vicegerent_mcp.py")
SPEC = importlib.util.spec_from_file_location("vicegerent_mcp", MODULE_PATH)
assert SPEC and SPEC.loader
vicegerent_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vicegerent_mcp)


class StoreHiddenSecretTests(unittest.TestCase):
    def test_value_is_prefixed_and_passed_only_on_stdin(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch.object(vicegerent_mcp.getpass, "getpass", return_value="token"),
            patch.object(vicegerent_mcp, "_thv_path", return_value="/usr/bin/thv"),
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed) as run,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = vicegerent_mcp._store_hidden_secret("elastic_key", "Elastic API key", "ApiKey ")

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            ["/usr/bin/thv", "secret", "set", "elastic_key"],
            input="ApiKey token",
            text=True,
            capture_output=True,
        )
        self.assertNotIn("token", stdout.getvalue() + stderr.getvalue())

    def test_toolhive_error_output_is_not_relayed(self) -> None:
        secret = "do-not-print-this"  # pragma: allowlist secret
        completed = subprocess.CompletedProcess([], 1, stdout=secret, stderr=secret)
        with (
            patch.object(vicegerent_mcp.getpass, "getpass", return_value=secret),
            patch.object(vicegerent_mcp, "_thv_path", return_value="thv"),
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = vicegerent_mcp._store_hidden_secret("api_key", "API key")

        self.assertEqual(result, 1)
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_blank_input_does_not_replace_existing_secret(self) -> None:
        with (
            patch.object(vicegerent_mcp.getpass, "getpass", return_value=""),
            patch.object(vicegerent_mcp.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = vicegerent_mcp._store_hidden_secret("api_key", "API key")

        self.assertIsNone(result)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
