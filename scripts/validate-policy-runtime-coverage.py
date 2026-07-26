#!/usr/bin/env python3
"""Require every Cerbos resource policy to have a runtime MCP probe."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / "charts" / "cerbos-policies" / "policies"
RUNTIME_SUITE = ROOT / "scripts" / "test-mcp-policies.sh"
RESOURCE_RE = re.compile(r"^\s*resource:\s*([A-Za-z0-9_]+)\s*$", re.MULTILINE)
PROBE_RE = re.compile(r'^\s*policy_probe\s+"([A-Za-z0-9_]+)"\s+', re.MULTILINE)


def fail(message: str) -> None:
    print(f"ERROR - {message}", file=sys.stderr)


def main() -> int:
    resources: list[str] = []
    malformed: list[str] = []
    for policy in sorted(POLICY_DIR.glob("*.yaml")):
        matches = RESOURCE_RE.findall(policy.read_text())
        if len(matches) != 1:
            malformed.append(f"{policy.relative_to(ROOT)} ({len(matches)} resource declarations)")
        else:
            resources.append(matches[0])

    probes = PROBE_RE.findall(RUNTIME_SUITE.read_text())
    resource_counts = Counter(resources)
    duplicate_resources = sorted(name for name, count in resource_counts.items() if count > 1)
    missing = sorted(set(resources) - set(probes))
    stale = sorted(set(probes) - set(resources))

    if malformed:
        fail("each policy file must declare exactly one resource: " + ", ".join(malformed))
    if duplicate_resources:
        fail("duplicate Cerbos resource policies: " + ", ".join(duplicate_resources))
    if missing:
        fail("Cerbos policies without runtime MCP probes: " + ", ".join(missing))
    if stale:
        fail("runtime MCP probes without Cerbos policies: " + ", ".join(stale))
    if malformed or duplicate_resources or missing or stale:
        return 1

    print(f"OK - all {len(resources)} Cerbos resource policies have runtime MCP probes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
