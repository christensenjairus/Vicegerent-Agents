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


def render_documents(agent_overrides: dict | None = None) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        defaults = Path(tmp) / "defaults.yaml"
        machine = Path(tmp) / "machine.yaml"
        defaults_result = subprocess.run(
            ["yq", ".agentDefaults", str(REPO / "values.defaults.yaml")],
            capture_output=True,
            text=True,
            check=True,
        )
        defaults.write_text(defaults_result.stdout, encoding="utf-8")
        agent_result = subprocess.run(
            ["yq", ".agents[0]", str(REPO / "values.example.yaml")],
            capture_output=True,
            text=True,
            check=True,
        )
        agent = yaml.safe_load(agent_result.stdout) or {}
        if agent_overrides:
            agent.update(agent_overrides)
        machine.write_text(yaml.safe_dump(agent), encoding="utf-8")
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
        return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_sandbox() -> dict:
    sandboxes = [
        document
        for document in render_documents()
        if document.get("kind") == "Sandbox"
    ]
    if len(sandboxes) != 1:
        die(f"expected exactly one Sandbox, found {len(sandboxes)}")
    return sandboxes[0]


def render_restart_job_name(agent_overrides: dict | None = None) -> str:
    jobs = [
        document
        for document in render_documents(agent_overrides)
        if document.get("kind") == "Job"
    ]
    if len(jobs) != 1:
        die(f"expected exactly one restart Job, found {len(jobs)}")
    return jobs[0]["metadata"]["name"]


def main() -> None:
    baseline_restart_job = render_restart_job_name()
    changed_config_restart_job = render_restart_job_name({"tuning": {"maxTurns": 101}})
    if baseline_restart_job == changed_config_restart_job:
        die("agent config changes must create a new restart Job so the gateway reloads them")

    pod_spec = render_sandbox()["spec"]["podTemplate"]["spec"]
    agent = pod_spec["containers"][0]
    env = {item["name"]: item.get("value") for item in agent["env"]}
    if env.get("OPENCODE_EXPERIMENTAL_LSP_TOOL") != "1":
        die("OpenCode's LSP tool must be enabled in the agent runtime")

    if pod_spec.get("hostUsers") is not False:
        die("Sandbox pods must use a private user namespace")
    if (
        pod_spec.get("securityContext", {}).get("seccompProfile", {}).get("type")
        != "RuntimeDefault"
    ):
        die("Sandbox pods must use the runtime-default seccomp profile")

    prepare = next(
        container
        for container in pod_spec["initContainers"]
        if container["name"] == "prepare-run"
    )
    prepare_security = prepare.get("securityContext", {})
    if not (
        prepare_security.get("runAsUser") == 0
        and prepare_security.get("runAsGroup") == 0
        and prepare_security.get("runAsNonRoot") is False
        and prepare_security.get("privileged") is False
        and prepare_security.get("allowPrivilegeEscalation") is False
        and prepare_security.get("readOnlyRootFilesystem") is True
        and set(prepare_security.get("capabilities", {}).get("drop", []))
        == {"ALL"}
        and set(prepare_security.get("capabilities", {}).get("add", []))
        == {"CHOWN", "DAC_OVERRIDE"}
    ):
        die("prepare-run must retain only the privileges required for volume ownership")

    for container in pod_spec["initContainers"] + pod_spec["containers"]:
        security = container.get("securityContext", {})
        if security.get("privileged") is True:
            die(f"{container['name']} must not be privileged")
        seccomp_type = security.get("seccompProfile", {}).get("type")
        if seccomp_type not in (None, "RuntimeDefault"):
            die(f"{container['name']} must not weaken the pod seccomp profile")
        if container["name"] == "prepare-run":
            continue
        capabilities = security.get("capabilities", {})
        if not (
            security.get("allowPrivilegeEscalation") is False
            and security.get("readOnlyRootFilesystem") is True
            and set(capabilities.get("drop", [])) == {"ALL"}
            and not capabilities.get("add")
        ):
            die(f"{container['name']} must be unprivileged and read-only")

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
