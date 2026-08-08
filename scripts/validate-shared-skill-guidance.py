#!/usr/bin/env python3
"""Assert shared operating guidance reaches every configured harness."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
REQUIRED_GUIDANCE: tuple[str, ...] = (
    "proactively save a compact declarative Mnemosyne memory in the same turn without asking for confirmation.",
    "Do not retain transient task state, raw logs, speculative conclusions, PR or issue identifiers, or duplicated documentation.",
    "Skills are shared procedural memory:",
    "canonical tree at `/opt/data/skills`",
    "Read a skill before modifying it",
    "Do not create skills for one-off task progress.",
    "Never use Git in the vault:",
    "Treat the vault as a self-saving folder; any scheduled Git backup is operator-managed outside the agent's vault workflow.",
    "All pull requests and merge requests are forcibly kept as drafts by the platform. This is expected.",
    "Apply the KISS principle: break work into simple, focused pieces and prefer the simplest solution that satisfies the stated requirements.",
    "Before creating a worktree from a persistent `/workspace/<repo>` clone, run `git worktree prune`",
    "Prefer the current `upstream/HEAD`, then `upstream/main` or `upstream/master`",
    "Never pull, reset, rebase, clean, delete branches, or automatically update an existing task worktree as part of hygiene",
)
FORBIDDEN_GUIDANCE: tuple[str, ...] = (
    "shared git-synced Obsidian vault",
    "After medium-to-large vault changes, commit and push the vault",
    "Treat the vault's `vicegerent` branch as its primary branch",
)
HARNESS_PROMPTS = {
    "Hermes": ("agent-soul", "SOUL.md"),
    "Codex": ("agent-codex-config", "config.toml"),
    "Claude Code": ("agent-claude-config", "CLAUDE.md"),
    "OpenCode": ("agent-opencode-config", "AGENTS.md"),
}


def die(message: str) -> None:
    print(f"FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


def values_slice(source: Path, expression: str, destination: Path) -> None:
    result = subprocess.run(
        ["yq", expression, str(source)], capture_output=True, text=True
    )
    if result.returncode:
        die(f"yq {expression} {source.name} failed: {result.stderr.strip()}")
    destination.write_text(result.stdout, encoding="utf-8")


def render_configmaps() -> dict[str, dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        defaults = Path(tmp) / "defaults.yaml"
        machine = Path(tmp) / "machine.yaml"
        values_slice(REPO / "values.defaults.yaml", ".agentDefaults", defaults)
        # The starter keeps Obsidian opt-in. Enable a vault only in this fixture so
        # the rendered prompts exercise the conditional vault guidance.
        values_slice(
            REPO / "values.example.yaml",
            '.agents[0] * {"obsidian": {"vaultPath": "/workspace/knowledge-vault"}}',
            machine,
        )
        result = subprocess.run(
            [
                "helm",
                "template",
                "agent",
                str(REPO / "charts/agent"),
                "-f",
                str(defaults),
                "-f",
                str(machine),
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode:
        die(f"helm template failed: {result.stderr.strip()[:400]}")
    return {
        document["metadata"]["name"]: document["data"]
        for document in yaml.safe_load_all(result.stdout)
        if document and document.get("kind") == "ConfigMap"
    }


def main() -> None:
    configmaps = render_configmaps()
    for harness, (name, key) in HARNESS_PROMPTS.items():
        prompt = configmaps.get(name, {}).get(key)
        if not isinstance(prompt, str):
            die(f"{harness} prompt missing from rendered ConfigMap {name}/{key}")
        assert isinstance(prompt, str)
        missing = [phrase for phrase in REQUIRED_GUIDANCE if phrase not in prompt]
        if missing:
            die(f"{harness} prompt lacks shared skill guidance: {missing}")
        forbidden = [phrase for phrase in FORBIDDEN_GUIDANCE if phrase in prompt]
        if forbidden:
            die(f"{harness} prompt retains forbidden vault Git guidance: {forbidden}")
    print("OK - shared operating guidance reaches all four harnesses")


if __name__ == "__main__":
    main()
