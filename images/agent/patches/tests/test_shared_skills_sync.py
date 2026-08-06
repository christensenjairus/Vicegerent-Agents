#!/usr/bin/env python3
"""Behavioral regression test for the shared-skills sync script
(``images/agent/skills-scripts/sync-shared-skills.sh``).

Runs the script against throwaway fixture trees. Needs no Hermes install --
only bash and python3 -- so it is safe to run in CI. Each assertion names the
guarantee it protects; see the MR for why each one exists.

    python3 test_shared_skills_sync.py
    VICEGERENT_SYNC_SCRIPT=/path/to/sync.sh python3 test_shared_skills_sync.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_SRC = REPO_ROOT / "images" / "agent" / "skills-scripts" / "sync-shared-skills.sh"

def _extract_script() -> str:
    """Read the script the Dockerfile bakes into /usr/local/bin."""
    override = os.environ.get("VICEGERENT_SYNC_SCRIPT")
    return Path(override or SCRIPT_SRC).read_text(encoding="utf-8")

def _skill(path: Path, name: str, body: str = "probe") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n\n# {name}\n", encoding="utf-8"
    )

def _run(script: Path, canonical: Path, agents: Path, farm: Path):
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "AGENT_SKILLS_DIR": str(canonical),
            "AGENTS_SKILLS_ROOT": str(agents),
            "CLAUDE_SKILLS_FARM": str(farm),
        },
    )
    assert proc.returncode == 0, f"sync exited {proc.returncode}: {proc.stderr}"
    try:
        json.loads(proc.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"stdout is not valid JSON ({exc}) — the post_tool_call wire "
            f"protocol warns on anything else. stdout={proc.stdout!r}"
        )
    return proc

def main() -> int:
    script_src = _extract_script()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        script = root / "sync.sh"
        script.write_text(script_src, encoding="utf-8")
        script.chmod(0o755)

        # Syntax first: a broken heredoc in the chart should fail loudly here.
        chk = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert chk.returncode == 0, f"bash -n failed: {chk.stderr}"

        canonical = root / "skills"
        agents = root / "agents" / "skills"
        farm = root / "claude"

        _skill(canonical / "flat-one", "flat-one")
        _skill(canonical / "github" / "code-review", "code-review")
        _skill(canonical / "mlops" / "inference" / "vllm", "vllm")
        # A preserved SKILL.md inside a support package: documentation, not a skill.
        _skill(canonical / "github" / "code-review" / "references" / "legacy", "legacy")
        farm.mkdir(parents=True, exist_ok=True)
        _skill(farm / "claude-authored", "claude-authored")
        (farm / "ghost").symlink_to(canonical / "gone")

        _run(script, canonical, agents, farm)

        assert (agents).is_symlink(), "~/.agents/skills is not a symlink"
        assert agents.resolve() == canonical.resolve(), "agents root points elsewhere"
        for name in ("flat-one", "code-review", "vllm"):
            link = farm / name
            assert link.is_symlink(), f"{name} not published to the farm"
            assert (link / "SKILL.md").exists(), f"{name} link does not resolve"
        assert not (farm / "legacy").exists(), (
            "a SKILL.md inside references/ was published as a skill"
        )
        assert not (farm / "ghost").exists(), "stale farm link was not pruned"
        print("  ok  publish: flattened, support packages skipped, stale pruned")

        adopted = canonical / "harness-authored" / "claude-authored"
        assert adopted.is_symlink(), (
            "a Claude-authored skill was not adopted into the canonical tree — "
            "Codex/OpenCode would never see it"
        )
        assert (adopted / "SKILL.md").exists(), "adopted link does not resolve"
        assert (farm / "claude-authored").is_dir()
        assert not (farm / "claude-authored").is_symlink(), (
            "the owner's real directory was replaced by one of our links"
        )
        print("  ok  adopt: harness-authored skill reaches the shared root")

        found = subprocess.run(
            ["find", "-L", str(canonical), "-name", "SKILL.md"],
            capture_output=True, text=True, timeout=60,
        )
        assert found.returncode == 0, f"find -L failed (symlink loop?): {found.stderr}"
        # 3 canonical skills + 1 preserved doc + 1 adopted = 5 reachable
        n = len([x for x in found.stdout.split() if x])
        assert n == 5, f"expected 5 reachable SKILL.md via -L, got {n}"
        print("  ok  no symlink loop between farm and adopt category")

        second = _run(script, canonical, agents, farm)
        assert "0 published, 0 adopted" in second.stderr, (
            f"second run was not a no-op: {second.stderr.strip()}"
        )
        print("  ok  idempotent: second run writes nothing")

        _skill(canonical / "misc" / "claude-authored", "claude-authored")
        third = _run(script, canonical, agents, farm)
        assert not (farm / "claude-authored").is_symlink(), (
            "name collision overwrote the owner's real directory"
        )
        assert "left to their owner" in third.stderr
        # A count alone is not actionable: the operator needs to know WHICH
        # skill is shadowed, or a silently-hidden skill stays invisible.
        assert "claude-authored" in third.stderr, (
            "collision reported as a bare count; the shadowed skill was not "
            f"named: {third.stderr.strip()}"
        )
        print("  ok  ownership: colliding name left to its owner, and named")

        import shutil

        shutil.rmtree(farm / "claude-authored")
        _run(script, canonical, agents, farm)
        assert not adopted.exists(), "adopted link survived deletion of its source"
        print("  ok  adopted link pruned when its source is deleted")

        # The curator archives a skill by MOVING it to skills/.archive/<name>.
        # Hermes then hides it (is_excluded_skill_path), so the shim must too --
        # otherwise archiving is a no-op for the other harnesses and the skill
        # stays live for them forever. Note .archive sits at depth 1, so a
        # `-mindepth 2 ... -name '.*' -prune` never evaluates it; the prune has
        # to match by path.
        archived = canonical / ".archive" / "archived-skill"
        _skill(archived, "archived-skill")
        fourth = _run(script, canonical, agents, farm)
        assert not (farm / "archived-skill").exists(), (
            "a curator-archived skill was re-published to the shared farm; "
            "archiving must remove it from the other harnesses too"
        )
        assert "archived-skill" not in fourth.stderr
        print("  ok  curator-archived skill is not re-published")

    print("all checks passed")
    return 0

if __name__ == "__main__":
    sys.exit(main())
