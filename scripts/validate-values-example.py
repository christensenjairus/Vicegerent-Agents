#!/usr/bin/env python3
"""Assert that values.example.yaml is a scoped Moveworks DevOps starter."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Never

import yaml

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "values.example.yaml"
DEFAULTS = REPO / "values.defaults.yaml"


def die(message: str) -> Never:
    print(f"FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        die(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> None:
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    defaults = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))

    policy = example.get("policy", {})
    source_control = policy.get("sourceControl", {})
    github = source_control.get("github", {})

    require_equal(
        github.get("allowedRepos"),
        [
            "moveworks-emu/moveworks",
            "moveworks-emu/k8s-manifests",
            "your-github-username/moveworks",
            "your-github-username/k8s-manifests",
            "christensenjairus/vicegerent-agents",
            "your-github-username/vicegerent-agents",
        ],
        "starter GitHub repository allowlist",
    )
    require_equal(
        github.get("forkRequiredOwners"),
        ["moveworks-emu", "christensenjairus"],
        "owners requiring fork-qualified pull-request heads",
    )
    require_equal(
        github.get("forkOwners"),
        ["your-github-username"],
        "allowed fork owners",
    )
    require_equal(github.get("username"), "your-github-username", "GitHub identity")

    if "gitlab" in source_control:
        die("the starter must not configure private GitLab MCP access")
    work_management = policy.get("workManagement", {})
    require_equal(
        work_management.get("jira"),
        {
            "allowedProjects": ["CHANGE"],
            "allowedAssignees": ["you@moveworks.ai"],
            "allowedIssueTypes": [
                "Environment Change",
                "Deploy Service Specific",
                "Service Restart",
            ],
        },
        "Moveworks DevOps Jira policy",
    )
    require_equal(
        work_management.get("linear"),
        {
            "allowedTeams": [
                "6deab0c5-9bda-4f82-b552-41f4aa9e449b",
                "DevOps",
                "DEVOPS",
            ],
            "allowedAssignees": ["you@moveworks.ai", "Your Name"],
        },
        "Moveworks DevOps Linear policy",
    )
    require_equal(
        work_management.get("pagerduty", {}).get("allowedServiceIds"),
        ["PJD41LB", "PV3S4O5", "PORBNK3", "P7X89U2"],
        "Moveworks DevOps PagerDuty service allowlist",
    )
    require_equal(
        policy.get("notion"),
        {
            "scratchpadPageId": "YOUR_NOTION_SCRATCHPAD_PAGE_ID",
            "allowedParentPageIds": [
                "6fc3f07af7084089a14088581502e3f1",  # pragma: allowlist secret
                "1de588d8909f80458ad6c0a831284768",  # pragma: allowlist secret
                "306588d8909f806d9e78f7004cf3b9db",  # pragma: allowlist secret
                "41348461628a472bbc16c14e2e866a89",  # pragma: allowlist secret
            ],
            "userId": "YOUR_NOTION_USER_ID",
        },
        "Moveworks Notion parent scope with personal placeholders",
    )
    require_equal(
        policy.get("alertmanager"),
        {"maxSilenceSeconds": 86400, "createdBy": "your-username"},
        "24-hour Moveworks Alertmanager policy",
    )
    require_equal(
        policy.get("dataAccess"),
        {
            "grafana": {
                "deniedDatasourceUids": ["fess5o6x6evb4b"],
                "deniedDatasourceNames": ["dev-opensearch-datasource"],
            },
            "elastic": {"deniedIndexPatterns": ["snowflake"]},
        },
        "Moveworks-wide Grafana and Elastic blocklists",
    )

    require_equal(
        policy.get("contentSafety"),
        {
            "moderation": {"status": "disabled"},
            "promptInjection": {"status": "disabled"},
        },
        "explicitly disabled OpenAI-backed content-safety checks",
    )

    agents = example.get("agents", [])
    require_equal(len(agents), 1, "starter agent count")
    agent = agents[0]
    require_equal(agent.get("name"), "my-first-agent", "starter agent name")
    require_equal(
        agent.get("git"),
        {"userName": "Your Name", "userEmail": "you@moveworks.ai"},
        "starter git identity",
    )
    require_equal(
        agent.get("providers", {}).get("openai", {}).get("enabled"),
        False,
        "agent OpenAI provider switch",
    )
    require_equal(agent.get("failover", {}).get("provider"), "", "OpenAI failover")
    require_equal(
        defaults.get("agentDefaults", {}).get("directEgress", {}).get("ssh"),
        {"hosts": {}},
        "empty default SSH host map",
    )
    require_equal(
        agent.get("directEgress", {}).get("ssh"),
        {"hosts": {"github.com": {"cnameChain": []}}},
        "explicit GitHub-only SSH host map",
    )
    require_equal(
        agent.get("directEgress", {}).get("slackFQDNs"),
        [
            "slack.com",
            "wss-primary.slack.com",
            "wss-backup.slack.com",
            "files.slack.com",
        ],
        "Slack direct-egress endpoints",
    )
    require_equal(
        agent.get("config", {}).get("agent", {}).get("system_prompt"),
        "You are a technical expert. Provide detailed, accurate technical information.",
        "starter system prompt",
    )
    expected_soul = """# Personality
You are a pragmatic senior engineer with strong taste.
You optimize for truth, clarity, and usefulness over politeness theater.

## Style
- Be direct without being cold
- Prefer substance over filler
- Push back when something is a bad idea
- Admit uncertainty plainly
- Keep explanations compact unless depth is useful

## What to avoid
- Sycophancy
- Hype language
- Repeating the user's framing if it's wrong
- Overexplaining obvious things

## Technical posture
- Prefer simple systems over clever systems
- Care about operational reality, not idealized architecture
- Treat edge cases as part of the design, not cleanup

## Tools
- When a task needs external data or a real action, search your available tools for the right one and use it
- Don't guess, hand-wave, or give up when a tool could get the real answer

## Secondary MCPs
- Secondary MCPs access GovCloud. If one times out, tell the user that they still need to enable the Gov VPN; do not repeatedly retry it."""
    require_equal(agent.get("soul"), expected_soul, "starter soul")

    if agent.get("obsidian", {}).get("vaultPath"):
        die("Obsidian must be opt-in")

    require_equal(
        example.get("egress", {}).get("internalAllowedCIDRs"),
        ["10.230.0.0/16"],
        "internal network allowlist",
    )
    if "artifactory.global.mgmt.moveworks.io" not in example.get("egress", {}).get(
        "exactDomains", []
    ):
        die("Moveworks Artifactory must be present in the exact-domain allowlist")
    require_equal(
        example.get("models", {}).get("openai", {}).get("enabled"),
        False,
        "platform openai backend switch",
    )
    for provider in ("deepseek", "zai"):
        require_equal(
            defaults.get("models", {}).get(provider, {}).get("enabled"),
            False,
            f"default platform {provider} backend switch",
        )
        if provider in example.get("models", {}):
            die(f"starter must inherit the disabled {provider} platform default")

    print("PASS - values.example.yaml is a scoped Moveworks DevOps starter")


if __name__ == "__main__":
    main()
