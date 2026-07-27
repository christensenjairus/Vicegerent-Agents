#!/usr/bin/env python3
"""Verify rendered per-provider reasoning configuration.

The agent chart maps each enabled provider's configured model and
``reasoningEffort`` to ``agent.reasoning_overrides``. This test guards that
mapping without prescribing a particular effort for a particular model.

Usage:
    python3 test_provider_reasoning_overrides.py \
        --chart-dir /path/to/charts/agent \
        --values /path/to/values.defaults.yaml
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile

import yaml


def load_agent_values(values_path: str) -> dict:
    with open(values_path, encoding="utf-8") as values_file:
        loaded = yaml.safe_load(values_file) or {}
    if "agentDefaults" in loaded:
        return loaded["agentDefaults"]
    if loaded.get("agents"):
        return loaded["agents"][0]
    return loaded


def render_config(chart_dir: str, agent_values: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as temp_file:
        yaml.safe_dump(agent_values, temp_file)
        temp_path = temp_file.name
    try:
        output = subprocess.run(
            [
                "helm",
                "template",
                "hermes",
                chart_dir,
                "-f",
                temp_path,
                "--show-only",
                "templates/config.yaml",
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    finally:
        os.unlink(temp_path)
    rendered = yaml.safe_load(output)
    return yaml.safe_load(rendered["data"]["config.yaml"])


def expected_overrides(agent_values: dict) -> dict[str, str]:
    expected: dict[str, str] = {}
    providers = agent_values.get("providers") or {}
    if not isinstance(providers, dict):
        raise AssertionError(f"providers must be a map, got {providers!r}")
    for name, provider in providers.items():
        if not isinstance(provider, dict) or not provider.get("enabled"):
            continue
        model = provider.get("model")
        effort = provider.get("reasoningEffort")
        if not model or effort is None:
            continue
        if model in expected and expected[model] != effort:
            raise AssertionError(
                f"enabled providers configure conflicting reasoning efforts for {model!r}"
            )
        expected[model] = effort
    if not expected:
        raise AssertionError("no enabled provider defines both model and reasoningEffort")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chart-dir", default="charts/agent")
    parser.add_argument("--values", default="values.defaults.yaml")
    args = parser.parse_args()

    agent_values = load_agent_values(args.values)
    expected = expected_overrides(agent_values)
    rendered = render_config(args.chart_dir, agent_values)
    actual = (rendered.get("agent") or {}).get("reasoning_overrides") or {}
    if actual != expected:
        raise AssertionError(
            f"agent.reasoning_overrides = {actual!r}; expected {expected!r}"
        )

    print(f"PASS - rendered {len(actual)} provider reasoning override(s) match configured efforts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
