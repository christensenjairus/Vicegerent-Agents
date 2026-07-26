#!/usr/bin/env python3
"""Assert the rendered Slack access gate is single-operator and DM-only."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import yaml

REPO = Path(__file__).resolve().parent.parent


def die(message: str) -> None:
    print(f"FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


def render_gate() -> tuple[str, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        defaults = Path(tmp) / "defaults.yaml"
        machine = Path(tmp) / "machine.yaml"
        for source, expression, target in (
            (REPO / "values.defaults.yaml", ".agentDefaults", defaults),
            (REPO / "values.example.yaml", ".agents[0]", machine),
        ):
            result = subprocess.run(
                ["yq", expression, str(source)], capture_output=True, text=True, check=True
            )
            target.write_text(result.stdout, encoding="utf-8")
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
        if result.returncode != 0:
            die(f"helm template failed: {result.stderr.strip()[:400]}")
        sandbox = next(
            (
                document
                for document in yaml.safe_load_all(result.stdout)
                if document and document.get("kind") == "Sandbox"
            ),
            None,
        )
    if sandbox is None:
        die("rendered chart has no Sandbox")
    sandbox = cast(dict[str, Any], sandbox)
    gate = next(
        (
            container
            for container in sandbox["spec"]["podTemplate"]["spec"]["initContainers"]
            if container["name"] == "validate-slack-access"
        ),
        None,
    )
    if gate is None:
        die("rendered Sandbox has no validate-slack-access init container")
    gate = cast(dict[str, Any], gate)
    return gate["args"][0], gate


def run_gate(script: str, env: dict[str, str]) -> bool:
    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> None:
    script, gate = render_gate()
    security = gate.get("securityContext", {})
    if not (
        security.get("allowPrivilegeEscalation") is False
        and security.get("readOnlyRootFilesystem") is True
        and security.get("runAsNonRoot") is True
        and security.get("capabilities", {}).get("drop") == ["ALL"]
    ):
        die("Slack access gate must be unprivileged and read-only")
    ref = gate.get("envFrom", [{}])[0].get("secretRef", {})
    if not ref.get("name") or ref.get("optional") is not False:
        die("Slack access gate must require the agent Secret")

    operator_id = "U0123" + "456789"
    other_user_id = "U9876" + "543210"
    direct_message_id = "D0123" + "456789"
    valid = {
        "SLACK_BOT_TOKEN": "bot-token",
        "SLACK_APP_TOKEN": "app-token",
        "SLACK_ALLOWED_USERS": operator_id,
        "SLACK_HOME_CHANNEL": direct_message_id,
    }
    cases = {
        "slack-disabled": ({}, True),
        "single-operator-dm": (valid, True),
        "multiple-users": ({**valid, "SLACK_ALLOWED_USERS": f"{operator_id},{other_user_id}"}, False),
        "missing-home": ({key: value for key, value in valid.items() if key != "SLACK_HOME_CHANNEL"}, False),
        "group-home": ({**valid, "SLACK_HOME_CHANNEL": "C0123" + "456789"}, False),
        "slack-allow-all": ({**valid, "SLACK_ALLOW_ALL_USERS": "true"}, False),
        "gateway-allowlist": ({**valid, "GATEWAY_ALLOWED_USERS": other_user_id}, False),
        "gateway-allow-all": ({**valid, "GATEWAY_ALLOW_ALL_USERS": "yes"}, False),
    }
    for name, (env, expected) in cases.items():
        actual = run_gate(script, env)
        if actual != expected:
            die(f"{name}: expected gate result {expected}, got {actual}")
    print("OK - Slack access is single-operator and DM-only")


if __name__ == "__main__":
    main()
