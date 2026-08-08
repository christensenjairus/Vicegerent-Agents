#!/usr/bin/env python3
"""Reject agent provider routes whose platform backend is disabled."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

PROVIDERS = ("anthropic", "openai", "deepseek", "zai")


def merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return deepcopy(override)
    merged = deepcopy(base)
    for key, value in override.items():
        merged[key] = merge(merged[key], value) if key in merged else deepcopy(value)
    return merged


def helm_enabled(value: object) -> bool:
    """Match Go-template truthiness so quoted booleans cannot bypass this check."""
    return bool(value)


def misaligned_routes(defaults: dict, machine: dict) -> list[str]:
    platform = merge(defaults, machine)
    agent_defaults = defaults.get("agentDefaults", {})
    failures: list[str] = []
    for index, configured_agent in enumerate(machine.get("agents", [])):
        agent = merge(agent_defaults, configured_agent)
        for provider in PROVIDERS:
            route_enabled = helm_enabled(
                agent.get("providers", {}).get(provider, {}).get("enabled")
            )
            backend_enabled = helm_enabled(
                platform.get("models", {}).get(provider, {}).get("enabled")
            )
            if route_enabled and not backend_enabled:
                name = configured_agent.get("name") or f"agents[{index}]"
                failures.append(
                    f"{name}: agents[].providers.{provider}.enabled is truthy to Helm but "
                    f"models.{provider}.enabled is not true"
                )
    return failures


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults", type=Path, required=True)
    parser.add_argument("values", nargs="+", type=Path)
    args = parser.parse_args()

    defaults = load(args.defaults)
    failures: list[str] = []
    for values in args.values:
        for failure in misaligned_routes(defaults, load(values)):
            failures.append(f"{values}: {failure}")

    if failures:
        for failure in failures:
            print(f"FAIL - {failure}")
        print(
            "Enable the matching models.<provider>.enabled platform backend or "
            "disable the agent provider route."
        )
        return 1

    print(
        "PASS - enabled agent provider routes have matching platform model backends"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
