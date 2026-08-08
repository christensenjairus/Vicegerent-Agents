#!/usr/bin/env python3
"""Regression tests for the merged machine-values configuration contract."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "values.defaults.yaml"
VALIDATOR = ROOT / "scripts/validate-machine-values.py"


def validate(values: dict) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temporary:
        machine = Path(temporary) / "values.yaml"
        machine.write_text(yaml.safe_dump(values), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "--defaults", str(DEFAULTS), str(machine)],
            capture_output=True,
            check=False,
            text=True,
        )


def test_rejects_unknown_and_mistyped_machine_values() -> None:
    result = validate(
        {
            "agents": [{"name": "valid-agent", "providers": {"openai": {"enabled": "false"}}}],
            "models": {"openai": {"enabled": False}},
            "unexpected": True,
        }
    )

    assert result.returncode == 1
    assert "unexpected: unknown key" in result.stderr
    assert "agents[0].providers.openai.enabled: expected boolean" in result.stderr


def test_rejects_duplicate_or_invalid_agent_release_identities() -> None:
    result = validate(
        {
            "agents": [
                {"name": "valid-agent"},
                {"name": "a" * 54},
                {"name": "Bad_Name"},
                {"name": "valid-agent"},
            ],
        }
    )

    assert result.returncode == 1
    assert "agents[1].name: must be a valid Kubernetes/Helm release name" in result.stderr
    assert "agents[2].name: must be a valid Kubernetes/Helm release name" in result.stderr
    assert "agents[3].name: duplicates agents[0].name" in result.stderr


def test_rejects_consumers_without_enabled_platform_capacity() -> None:
    result = validate(
        {
            "policy": {"contentSafety": {"moderation": {"status": "enabled"}}},
            "models": {"anthropic": {"enabled": False}, "openai": {"enabled": False}},
            "agents": [
                {
                    "name": "valid-agent",
                    "providers": {"openai": {"enabled": False}},
                    "failover": {"provider": "openai"},
                    "harnesses": {"codex": "gpt-5.6-sol"},
                }
            ],
        }
    )

    assert result.returncode == 1
    assert "policy.contentSafety.moderation.status requires models.openai.enabled" in result.stderr
    assert "agents[0].failover.provider requires models.openai.enabled" in result.stderr
    assert "agents[0].harnesses.claudeCode requires models.anthropic.enabled" in result.stderr


def test_applies_machine_agent_defaults_to_capacity_checks() -> None:
    result = validate(
        {
            "agentDefaults": {
                "providers": {"deepseek": {"enabled": True}},
                "failover": {"provider": "openai"},
            },
            "models": {"deepseek": {"enabled": False}, "openai": {"enabled": False}},
            "agents": [{"name": "valid-agent"}],
        }
    )

    assert result.returncode == 1
    assert "agents[0].providers.deepseek.enabled requires models.deepseek.enabled" in result.stderr
    assert "agents[0].failover.provider requires models.openai.enabled" in result.stderr


def test_rejects_invalid_default_ssh_host_contracts() -> None:
    result = validate(
        {
            "agentDefaults": {
                "directEgress": {
                    "ssh": {"hosts": {"git.example.com": {"cnameChain": "not-a-list", "extra": True}}}
                }
            },
            "agents": [{"name": "valid-agent"}],
        }
    )

    assert result.returncode == 1
    assert "agentDefaults.directEgress.ssh.hosts.git.example.com.extra: unknown key" in result.stderr
    assert "agentDefaults.directEgress.ssh.hosts.git.example.com.cnameChain: expected list of strings" in result.stderr


def test_rejects_invalid_content_safety_statuses() -> None:
    result = validate(
        {
            "agents": [{"name": "valid-agent"}],
            "policy": {"contentSafety": {"promptInjection": {"status": "enabledish"}}},
        }
    )

    assert result.returncode == 1
    assert "policy.contentSafety.promptInjection.status: expected one of enabled, disabled" in result.stderr


def test_reports_structural_shape_errors_without_cross_field_tracebacks() -> None:
    for values, expected in (
        ({"agents": None}, "agents: expected list"),
        (
            {"policy": {"contentSafety": {"moderation": None}}},
            "policy.contentSafety.moderation: expected mapping",
        ),
    ):
        result = validate(values)

        assert result.returncode == 1
        assert expected in result.stderr
        assert "Traceback" not in result.stderr


def test_accepts_the_starter_contract() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--defaults",
            str(DEFAULTS),
            str(ROOT / "values.example.yaml"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "PASS - merged machine values satisfy the configuration contract" in result.stdout


def main() -> int:
    test_rejects_unknown_and_mistyped_machine_values()
    test_rejects_duplicate_or_invalid_agent_release_identities()
    test_rejects_consumers_without_enabled_platform_capacity()
    test_applies_machine_agent_defaults_to_capacity_checks()
    test_rejects_invalid_default_ssh_host_contracts()
    test_rejects_invalid_content_safety_statuses()
    test_reports_structural_shape_errors_without_cross_field_tracebacks()
    test_accepts_the_starter_contract()
    print("OK - merged machine-values configuration contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
