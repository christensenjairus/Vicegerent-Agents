#!/usr/bin/env python3
"""Behavioral regression test for Slack /chatter DM-scoped defaults."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile



HELPER_NAME = "_slack_dm_scoped_display_default"


class Platform:
    SLACK = "slack"
    TELEGRAM = "telegram"


@dataclass
class Source:
    platform: str
    chat_id: str | None


def _load_helper(run_path: Path):
    tree = ast.parse(run_path.read_text(encoding="utf-8"), filename=str(run_path))
    helper = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == HELPER_NAME
        ),
        None,
    )
    if helper is None:
        raise AssertionError(f"patched gateway/run.py does not define {HELPER_NAME}")
    module = ast.Module(body=[helper], type_ignores=[])
    namespace = {"Platform": Platform}
    exec(compile(module, str(run_path), "exec"), namespace)
    return namespace[HELPER_NAME]


def _apply_patch(source_root: Path, destination: Path, patch: Path) -> None:
    for package in ("agent", "gateway", "hermes_cli", "locales"):
        shutil.copytree(source_root / package, destination / package)

    env = {
        **os.environ,
        "PYTHONPATH": str(destination),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result: subprocess.CompletedProcess[str] | None = None
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
    assert result is not None
    if "already applied" not in result.stdout:
        raise AssertionError("patch 0041 is not idempotent")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
    patch = Path(__file__).resolve().parents[1] / "0041-interim-assistant-messages-session-toggle.py"

    template = (repo_root / "charts" / "agent" / "templates" / "_helpers.tpl").read_text(
        encoding="utf-8"
    )
    slack_block = template.split("    slack:\n", 1)[1].split("approvals:\n", 1)[0]
    assert "      interim_assistant_messages: true\n" in slack_block, (
        "Slack chatter must be configured on so DMs inherit an enabled default"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        _apply_patch(source_root, root, patch)
        helper = _load_helper(root / "gateway" / "run.py")

        cases = (
            ("Slack DM enabled config", Platform.SLACK, "D123", True, True),
            ("Slack DM disabled config", Platform.SLACK, "D123", False, False),
            ("Slack public channel", Platform.SLACK, "C123", True, False),
            ("Slack private channel", Platform.SLACK, "G123", True, False),
            ("Slack missing channel ID", Platform.SLACK, None, True, False),
            ("non-Slack enabled config", Platform.TELEGRAM, "123", True, True),
            ("non-Slack disabled config", Platform.TELEGRAM, "123", False, False),
        )
        for label, platform, chat_id, configured, expected in cases:
            source = Source(platform=platform, chat_id=chat_id)
            actual = helper(source, configured)
            assert actual is expected, f"{label}: expected {expected}, got {actual}"

        run_tree = ast.parse((root / "gateway" / "run.py").read_text(encoding="utf-8"))
        slash_tree = ast.parse(
            (root / "gateway" / "slash_commands.py").read_text(encoding="utf-8")
        )
        run_calls = [
            node
            for node in ast.walk(run_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == HELPER_NAME
        ]
        slash_calls = [
            node
            for node in ast.walk(slash_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == HELPER_NAME
        ]
        assert len(run_calls) == 1, "agent execution path must scope the config default"
        assert len(slash_calls) == 1, "first /chatter toggle must scope the config default"

    print("PASS: Slack chatter defaults on in DMs and off outside DMs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
