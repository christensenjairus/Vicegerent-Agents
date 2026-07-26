#!/usr/bin/env python3
"""Regression test for atomic model-switch prompt persistence patch 0047."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROBE = r'''
from pathlib import Path
from types import SimpleNamespace

from agent.chat_completion_helpers import rewrite_prompt_model_identity
from agent.conversation_loop import _restore_or_build_system_prompt
from hermes_state import SessionDB

db = SessionDB(Path(ROOT) / "state.db")
session_id = "model-switch-regression"
old_prompt = "Stable prefix\n\nModel: old-model\nProvider: old-provider"
db.create_session(session_id, "test", system_prompt=old_prompt)

switching = SimpleNamespace(_cached_system_prompt=old_prompt)
rewrite_prompt_model_identity(switching, "new-model", "new-provider")
refreshed = switching._cached_system_prompt
db.update_session_billing_route(
    session_id,
    provider="new-provider",
    base_url="https://new.example.test",
    billing_mode="anthropic_messages",
    system_prompt=refreshed,
)

stored = db.get_session(session_id)["system_prompt"]
if stored != refreshed:
    raise SystemExit(f"stored prompt mismatch: {stored!r}")
if "Model: new-model" not in stored or "Provider: new-provider" not in stored:
    raise SystemExit("stored prompt retains stale runtime identity")

builds = []
continuing = SimpleNamespace(
    _cached_system_prompt=None,
    _session_db=db,
    session_id=session_id,
    model="new-model",
    provider="new-provider",
    _build_system_prompt=lambda _message: builds.append(True) or "rebuilt",
)
_restore_or_build_system_prompt(
    continuing,
    None,
    [{"role": "user", "content": "continue"}],
)
if builds:
    raise SystemExit("next turn rebuilt the prompt and lost prefix-cache reuse")
if continuing._cached_system_prompt != refreshed:
    raise SystemExit("next turn did not restore the refreshed prompt verbatim")
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-fix", action="store_true")
    args = parser.parse_args()

    patch = Path(__file__).resolve().parents[1] / "0047-model-switch-prompt.py"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        shutil.copytree("/opt/hermes/agent", root / "agent")
        shutil.copy2("/opt/hermes/hermes_state.py", root / "hermes_state.py")
        env = {
            **os.environ,
            "HERMES_ROOT": str(root),
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        if not args.pre_fix:
            first = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if first.returncode != 0:
                raise SystemExit(f"FAIL: patch failed:\n{first.stderr}")
            second = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if second.returncode != 0 or "already applied" not in second.stdout:
                raise SystemExit("FAIL: patch is not idempotent")

        probe = subprocess.run(
            [sys.executable, "-c", f"ROOT={str(root)!r}\n{PROBE}"],
            env=env,
            text=True,
            capture_output=True,
        )
        if probe.returncode != 0:
            raise SystemExit(f"FAIL: model-switch interaction failed:\n{probe.stderr}")

    print("PASS: model switch persists refreshed identity without a next-turn rebuild")
    return 0


if __name__ == "__main__":
    sys.exit(main())
