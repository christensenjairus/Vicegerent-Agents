#!/usr/bin/env python3
"""Regression test for direct Slack delivery in the sandbox."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


FUNCTIONS = (
    "_resolve_slack_user_dm",
    "_standalone_send",
)


def _assert_slack_aware_proxy_resolution(adapter_path: Path) -> None:
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"), filename=str(adapter_path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS
    }
    assert set(functions) == set(FUNCTIONS), f"missing expected functions: {FUNCTIONS!r}"

    for name, function in functions.items():
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_resolve_slack_proxy_url"
        ]
        assert calls, (
            f"{name} must use _resolve_slack_proxy_url() so Slack honors NO_PROXY "
            "and bypasses Vicegerent's GET-only egress proxy"
        )

        generic_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_proxy_url"
        ]
        assert not generic_calls, (
            f"{name} must not directly use resolve_proxy_url(), which ignores "
            "NO_PROXY when target_hosts is omitted"
        )


def _apply_patch(source_root: Path, destination: Path, patch: Path) -> None:
    shutil.copytree(source_root / "plugins", destination / "plugins")
    slack_sdk_spec = importlib.util.find_spec("slack_sdk")
    if slack_sdk_spec is None or not slack_sdk_spec.submodule_search_locations:
        raise AssertionError("could not locate the installed slack_sdk package")
    slack_sdk_root = Path(next(iter(slack_sdk_spec.submodule_search_locations)))
    shutil.copytree(slack_sdk_root, destination / "slack_sdk")
    env = {
        **os.environ,
        "PYTHONPATH": str(destination),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for attempt in range(2):
        result = subprocess.run(
            [sys.executable, str(patch)],
            cwd="/",
            env=env,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise AssertionError(
                f"patch attempt {attempt + 1} failed\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        if attempt == 1 and "already applied" not in result.stdout:
            raise AssertionError("patch 0007 is not idempotent")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-fix", action="store_true")
    args = parser.parse_args()

    source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
    patch = Path(__file__).resolve().parents[1] / "0007-slack-bypass-egress-proxy.py"

    if args.pre_fix:
        _assert_slack_aware_proxy_resolution(
            source_root / "plugins" / "platforms" / "slack" / "adapter.py"
        )
    else:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            _apply_patch(source_root, root, patch)
            _assert_slack_aware_proxy_resolution(root / "plugins" / "platforms" / "slack" / "adapter.py")

    print("PASS: Slack delivery honors NO_PROXY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
