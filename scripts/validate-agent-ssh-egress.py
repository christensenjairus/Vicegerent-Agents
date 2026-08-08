#!/usr/bin/env python3
"""Verify empty SSH config fails closed and configured hosts stay FQDN-scoped."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Never

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULTS = REPO / "values.defaults.yaml"
EXPECTED_FQDNS = {
    "git.example.com",
    "edge.example.net",
    "origin.example.net",
}


def die(message: str) -> Never:
    print(f"FAIL - {message}")
    raise SystemExit(1)


def render(hosts: dict[str, object]) -> dict:
    defaults = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))["agentDefaults"]
    defaults["directEgress"]["ssh"]["hosts"] = hosts
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as values:
        yaml.safe_dump(defaults, values)
        values.flush()
        try:
            rendered = subprocess.run(
                ["helm", "template", "agent", "charts/agent", "-f", values.name],
                cwd=REPO,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        except subprocess.CalledProcessError as error:
            die(f"helm template failed: {error.stderr.strip()}")
    policies = [
        document
        for document in yaml.safe_load_all(rendered)
        if document
        and document.get("kind") == "CiliumNetworkPolicy"
        and document.get("metadata", {}).get("name") == "agent-egress"
    ]
    if len(policies) != 1:
        die(f"expected one rendered CiliumNetworkPolicy, got {len(policies)}")
    return policies[0]


def exposes_port(rule: dict, port: str) -> bool:
    return any(
        item.get("port") == port
        for group in rule.get("toPorts", [])
        for item in group.get("ports", [])
    )


def dns_names(policy: dict) -> set[str]:
    return {
        matcher["matchName"]
        for rule in policy["spec"]["egress"]
        for port_group in rule.get("toPorts", [])
        for dns_rule in port_group.get("rules", {}).get("dns", [])
        for matcher in [dns_rule]
        if "matchName" in matcher
    }


def require_scoped_ssh_rule(policy: dict, expected: set[str], label: str) -> None:
    ssh_rules = [
        rule for rule in policy["spec"]["egress"] if exposes_port(rule, "22")
    ]
    if len(ssh_rules) != 1:
        die(f"{label} rendered {len(ssh_rules)} TCP/22 rules")
    if not ssh_rules[0].get("toFQDNs"):
        die(f"{label} TCP/22 rule has no FQDN selector")
    actual = {
        matcher.get("matchName") for matcher in ssh_rules[0].get("toFQDNs", [])
    }
    if actual != expected:
        die(f"{label} SSH FQDNs are {sorted(actual)}, expected {sorted(expected)}")


def main() -> None:
    empty = render({})
    empty_ssh_rules = [
        rule for rule in empty["spec"]["egress"] if exposes_port(rule, "22")
    ]
    if empty_ssh_rules:
        die("empty directEgress.ssh.hosts rendered a TCP/22 egress rule")

    configured = render(
        {
            "git.example.com": {
                "cnameChain": ["edge.example.net", "origin.example.net"]
            }
        }
    )
    require_scoped_ssh_rule(configured, EXPECTED_FQDNS, "configured hosts")
    if not EXPECTED_FQDNS.issubset(dns_names(configured)):
        die("configured SSH host or CNAME chain is missing from DNS policy")

    bodyless = render({"git.example.com": None})
    require_scoped_ssh_rule(bodyless, {"git.example.com"}, "body-less host")
    if "git.example.com" not in dns_names(bodyless):
        die("body-less SSH host is missing from DNS policy")

    print("PASS - SSH egress is absent when unconfigured and FQDN-scoped when enabled")


if __name__ == "__main__":
    main()
