#!/usr/bin/env python3
"""Assert load-bearing agent runtime ownership in the rendered Sandbox."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def die(message: str) -> None:
    print(f"FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


def render_sandbox() -> dict:
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
        sandboxes = [
            document
            for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "Sandbox"
        ]
    if len(sandboxes) != 1:
        die(f"expected exactly one Sandbox, found {len(sandboxes)}")
    return sandboxes[0]


def main() -> None:
    pod_spec = render_sandbox()["spec"]["podTemplate"]["spec"]
    prepare = next(
        container
        for container in pod_spec["initContainers"]
        if container["name"] == "prepare-run"
    )
    script = prepare["args"][0]
    root_chown = "chown 10000:10000 /opt/data"
    if script.splitlines().count(root_chown) != 1:
        die("prepare-run must chown the /opt/data directory itself exactly once")
    first_child_setup = script.index("mkdir -p /opt/data/")
    if script.index(root_chown) > first_child_setup:
        die("prepare-run must own /opt/data before creating or repairing child directories")

    print("OK - agent runtime ownership is rendered")


if __name__ == "__main__":
    main()
