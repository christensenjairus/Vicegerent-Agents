#!/usr/bin/env python3
"""Assert skill-maintenance guidance reaches every configured harness."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
REQUIRED_GUIDANCE = (
    "Skills are shared procedural memory:",
    "Read a skill before modifying it",
    "Do not create skills for one-off task progress.",
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
        values_slice(REPO / "values.example.yaml", ".agents[0]", machine)
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
        missing = [phrase for phrase in REQUIRED_GUIDANCE if phrase not in prompt]
        if missing:
            die(f"{harness} prompt lacks shared skill guidance: {missing}")
    print("OK - shared skill-maintenance guidance reaches all four harnesses")


if __name__ == "__main__":
    main()
