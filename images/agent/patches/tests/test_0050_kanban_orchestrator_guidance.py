#!/usr/bin/env python3
"""Production-path regression test for Kanban orchestrator guidance."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DRIVER = r'''
import os
from pathlib import Path
from types import SimpleNamespace

from agent import prompt_builder, system_prompt


assert hasattr(prompt_builder, "KANBAN_ORCHESTRATOR_GUIDANCE"), (
    "agent.prompt_builder has no Kanban orchestrator guidance"
)
worker_guidance = prompt_builder.KANBAN_GUIDANCE
orchestrator_guidance = prompt_builder.KANBAN_ORCHESTRATOR_GUIDANCE
assert "kanban_show()` first" not in orchestrator_guidance
assert "Kanban task execution protocol" not in orchestrator_guidance
assert "kanban_create" in orchestrator_guidance
assert "unknown assignee" in orchestrator_guidance
assert "Route, don't implement." not in orchestrator_guidance
assert "Handle ordinary interactive work and routine coding tasks directly." in orchestrator_guidance
assert "does not override applicable coding-agent or delegation guidance" in orchestrator_guidance
assert "genuinely large tasks, explicit delegation requests, or independent review" in orchestrator_guidance
assert "Kanban is appropriate when the user requests it" in orchestrator_guidance
assert "Kanban task execution protocol" in worker_guidance


system_prompt._ra = lambda: SimpleNamespace(
    load_soul_md=lambda context_length: "",
    build_nous_subscription_prompt=lambda valid_tool_names: "",
    build_environment_hints=lambda: "",
)


def fake_agent(valid_tool_names):
    return SimpleNamespace(
        valid_tool_names=set(valid_tool_names),
        load_soul_identity=False,
        skip_context_files=True,
        _tool_use_enforcement=False,
        model="",
        provider="",
        platform="",
        _memory_store=None,
        _memory_manager=None,
        _memory_enabled=False,
        _user_profile_enabled=False,
        pass_session_id=False,
        session_id=None,
    )


def stable_prompt(valid_tool_names, task_id=None):
    previous = os.environ.pop("HERMES_KANBAN_TASK", None)
    try:
        if task_id is not None:
            os.environ["HERMES_KANBAN_TASK"] = task_id
        return system_prompt.build_system_prompt_parts(
            fake_agent(valid_tool_names)
        )["stable"]
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
        if previous is not None:
            os.environ["HERMES_KANBAN_TASK"] = previous


without_kanban = stable_prompt(set())
assert worker_guidance not in without_kanban
assert orchestrator_guidance not in without_kanban

worker = stable_prompt({"kanban_show"}, task_id="task-123")
assert worker_guidance in worker
assert orchestrator_guidance not in worker

orchestrator = stable_prompt({"kanban_show"})
assert orchestrator_guidance in orchestrator
assert worker_guidance not in orchestrator


root = Path(os.environ["HERMES_TEST_ROOT"])
agent_init_source = (root / "agent" / "agent_init.py").read_text(encoding="utf-8")
system_prompt_source = (root / "agent" / "system_prompt.py").read_text(
    encoding="utf-8"
)
assert "kanban_show tool is present iff HERMES_KANBAN_TASK is set" not in agent_init_source
assert "Normal chat sessions never see" not in system_prompt_source
assert "only present when the\n    # dispatcher spawned this process" not in system_prompt_source
'''


def _run_driver(root: Path, source_root: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HERMES_TEST_ROOT": str(root),
        "PYTHONPATH": f"{root}:{source_root}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", DRIVER],
        cwd="/",
        env=env,
        text=True,
        capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-fix", action="store_true")
    args = parser.parse_args()

    source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
    relative_paths = (
        Path("agent/prompt_builder.py"),
        Path("agent/agent_init.py"),
        Path("agent/system_prompt.py"),
    )
    watched = {
        source_root / relative: (source_root / relative).read_text(encoding="utf-8")
        for relative in relative_paths
    }
    patch = (
        Path(__file__).resolve().parents[1]
        / "0050-kanban-orchestrator-board-guidance.py"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        shutil.copytree(source_root / "agent", root / "agent")

        if not args.pre_fix:
            env = {
                **os.environ,
                "PYTHONPATH": f"{root}:{source_root}",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            first = subprocess.run(
                [sys.executable, str(patch)],
                cwd="/",
                env=env,
                text=True,
                capture_output=True,
            )
            if first.returncode:
                raise SystemExit(
                    "FAIL: first patch application failed\n"
                    f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}"
                )
            after_first = {
                relative: (root / relative).read_text(encoding="utf-8")
                for relative in relative_paths
            }
            second = subprocess.run(
                [sys.executable, str(patch)],
                cwd="/",
                env=env,
                text=True,
                capture_output=True,
            )
            if second.returncode:
                raise SystemExit(
                    "FAIL: second patch application failed\n"
                    f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
                )
            after_second = {
                relative: (root / relative).read_text(encoding="utf-8")
                for relative in relative_paths
            }
            if after_second != after_first:
                raise SystemExit("FAIL: patch 0050 is not idempotent")

        result = _run_driver(root, source_root)
        if result.returncode:
            raise SystemExit(
                "FAIL: production Kanban prompt path failed\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

    for path, before in watched.items():
        if path.read_text(encoding="utf-8") != before:
            raise SystemExit(f"FAIL: test mutated installed Hermes source: {path}")

    mode = "pre-fix" if args.pre_fix else "patched"
    print(f"PASS: patch 0050 {mode} production-path probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
