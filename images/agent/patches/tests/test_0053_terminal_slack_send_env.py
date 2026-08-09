#!/usr/bin/env python3
"""Regression test for terminal-side ``hermes send`` Slack credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


FEATURE_FLAG = "HERMES_TERMINAL_ALLOW_SLACK_SEND"
FORWARDED = {
    "SLACK_BOT_TOKEN": "bot-token",
    "SLACK_HOME_CHANNEL": "D00000000",  # pragma: allowlist secret
}
BLOCKED = {
    "SLACK_APP_TOKEN": "app-token",
    "SLACK_ALLOWED_USERS": "U01234567",
    "SLACK_SIGNING_SECRET": "signing-secret",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "provider-secret",  # pragma: allowlist secret
}


def _environment_values(
    root: Path, *, enabled: bool, function_name: str
) -> dict[str, str | None]:
    program = f"""
import json
import os
from tools.environments.local import {function_name}

base = json.loads(os.environ['TEST_BASE_ENV'])
result = {function_name}(base)
print(json.dumps({{key: result.get(key) for key in sorted(base)}}))
"""
    env = {
        **os.environ,
        "PYTHONPATH": f"{root}{os.pathsep}/opt/hermes",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEST_BASE_ENV": json.dumps({**FORWARDED, **BLOCKED}),
    }
    if enabled:
        env[FEATURE_FLAG] = "true"
    else:
        env.pop(FEATURE_FLAG, None)
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _assert_terminal_send_credentials(root: Path, function_name: str) -> None:
    enabled = _environment_values(root, enabled=True, function_name=function_name)
    assert {key: enabled[key] for key in FORWARDED} == FORWARDED, enabled
    assert {key: enabled[key] for key in BLOCKED} == {
        key: None for key in BLOCKED
    }, enabled

    disabled = _environment_values(root, enabled=False, function_name=function_name)
    assert {key: disabled[key] for key in FORWARDED | BLOCKED} == {
        key: None for key in FORWARDED | BLOCKED
    }, disabled


def _assert_all_terminal_paths(root: Path) -> None:
    for function_name in ("_sanitize_subprocess_env", "_make_run_env"):
        _assert_terminal_send_credentials(root, function_name)


def _apply_patch(source_root: Path, destination: Path, patch: Path) -> None:
    shutil.copytree(source_root / "tools", destination / "tools")
    env = {
        **os.environ,
        "PYTHONPATH": f"{destination}{os.pathsep}/opt/hermes",
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
        assert result.returncode == 0, result.stdout + result.stderr
        if attempt == 1:
            assert "already applied" in result.stdout, result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-fix", action="store_true")
    args = parser.parse_args()

    source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
    patch = Path(__file__).resolve().parents[1] / "0053-terminal-slack-send-env.py"

    if args.pre_fix:
        _assert_all_terminal_paths(source_root.parent)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            _apply_patch(source_root, root, patch)
            _assert_all_terminal_paths(root)

    print("PASS: terminal hermes send receives only required Slack credentials on every path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
