#!/usr/bin/env python3
"""Assert load-bearing agent runtime ownership in the rendered Sandbox."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Never

import yaml

REPO = Path(__file__).resolve().parent.parent


def die(message: str) -> Never:
    print(f"FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


def render_documents(
    agent_overrides: dict | None = None,
    values_file: Path = REPO / "values.example.yaml",
    release_name: str = "agent",
) -> list[dict]:
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
            ["yq", ".agents[0]", str(values_file)],
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
                release_name,
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


def render_sandbox(
    values_file: Path = REPO / "values.example.yaml",
    release_name: str = "agent",
) -> dict:
    sandboxes = [
        document
        for document in render_documents(values_file=values_file, release_name=release_name)
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


def validate_ssh_secret_migration() -> None:
    guide = (REPO / "docs/backup-and-restore.md").read_text(encoding="utf-8")
    start_marker = (
        "      (.data.agent_ed25519 // .data.hermes_agent_ed25519) "
        "as $private_key\n"
    )
    end_marker = "        end' \\\n  | kubectl --context kind-vicegerent create -f -"
    try:
        start = guide.index(start_marker)
        end = guide.index(end_marker, start) + len("        end")
    except ValueError:
        die("the agent rename guide must contain the SSH Secret migration filter")
    jq_filter = guide[start:end]

    for source_key in ("agent_ed25519", "hermes_agent_ed25519"):
        source = {"type": "Opaque", "data": {source_key: "fixture-private-key"}}
        result = subprocess.run(
            [
                "jq",
                "-e",
                "--arg",
                "destination",
                "bot-jchristensen-ssh-key",
                "--arg",
                "namespace",
                "agent-sandbox",
                jq_filter,
            ],
            input=json.dumps(source),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            die(f"SSH Secret migration must accept the {source_key} source key")
        migrated = json.loads(result.stdout)
        if migrated.get("data") != {"agent_ed25519": "fixture-private-key"}:
            die("SSH Secret migration must emit only the agent_ed25519 target key")

    missing = subprocess.run(
        [
            "jq",
            "-e",
            "--arg",
            "destination",
            "bot-jchristensen-ssh-key",
            "--arg",
            "namespace",
            "agent-sandbox",
            jq_filter,
        ],
        input=json.dumps({"type": "Opaque", "data": {}}),
        capture_output=True,
        text=True,
    )
    if missing.returncode == 0:
        die("SSH Secret migration must reject a source with no private key")


def main() -> None:
    baseline_restart_job = render_restart_job_name()
    changed_config_restart_job = render_restart_job_name({"tuning": {"maxTurns": 101}})
    if baseline_restart_job == changed_config_restart_job:
        die("agent config changes must create a new restart Job so the gateway reloads them")

    operator_name = "bot-jchristensen"
    for profile in (REPO / "examples/personal.yaml", REPO / "examples/work.yaml"):
        configured = yaml.safe_load(profile.read_text(encoding="utf-8"))
        release_name = configured["agents"][0]["name"]
        if release_name != operator_name:
            die(
                f"{profile.relative_to(REPO)} must name the operator's agent "
                f"'{operator_name}'"
            )
        sandbox = render_sandbox(profile, release_name)
        if sandbox["metadata"]["name"] != operator_name:
            die(f"{profile.relative_to(REPO)} must render the operator's Sandbox")
        pod_template = sandbox["spec"]["podTemplate"]
        if (
            pod_template["metadata"]["labels"].get("vicegerent.io/dashboard")
            != operator_name
        ):
            die(f"{profile.relative_to(REPO)} must label the operator's pod")
        container = pod_template["spec"]["containers"][0]
        if container["name"] != operator_name:
            die(f"{profile.relative_to(REPO)} must render the operator's container")

    alternate_name = "alternate-agent"
    alternate = render_sandbox(release_name=alternate_name)
    alternate_template = alternate["spec"]["podTemplate"]
    alternate_container = alternate_template["spec"]["containers"][0]
    if not (
        alternate["metadata"]["name"] == alternate_name
        and alternate_template["metadata"]["labels"].get("vicegerent.io/dashboard")
        == alternate_name
        and alternate_container["name"] == alternate_name
    ):
        die("Sandbox, pod label, and container identity must follow the Helm release name")

    log_values = yaml.safe_load(
        (REPO / "stages/values/victoria-logs.yaml").read_text(encoding="utf-8")
    )
    log_scope = log_values["vector"]["customConfig"]["transforms"]["scope"][
        "condition"
    ]["source"]
    required_log_scope = (
        '.kubernetes.pod_labels."vicegerent.io/dashboard"',
        'sandbox_agent != ""',
        "container == sandbox_agent",
    )
    if any(fragment not in log_scope for fragment in required_log_scope):
        die("VictoriaLogs must select each sandbox's release-named agent container")

    pod_spec = render_sandbox()["spec"]["podTemplate"]["spec"]
    agent = pod_spec["containers"][0]
    env = {item["name"]: item.get("value") for item in agent["env"]}
    if env.get("OPENCODE_EXPERIMENTAL_LSP_TOOL") != "1":
        die("OpenCode's LSP tool must be enabled in the agent runtime")
    if "-i /opt/agent-ssh/agent_ed25519 " not in env.get("GIT_SSH_COMMAND", ""):
        die("GIT_SSH_COMMAND must use the generic agent SSH key path")

    volume_mounts = {item["name"]: item for item in agent["volumeMounts"]}
    if volume_mounts["ssh-key"]["mountPath"] != "/opt/agent-ssh":
        die("the agent SSH key Secret must mount at /opt/agent-ssh")
    volumes = {item["name"]: item for item in pod_spec["volumes"]}
    if (
        volumes["ssh-key"]["secret"]["secretName"]
        != "agent-ssh-key"  # pragma: allowlist secret
    ):
        die("the agent Sandbox must consume the release-named SSH key Secret")
    secret_setup = (REPO / "scripts/install/setup-secrets-agent.sh").read_text(
        encoding="utf-8"
    )
    if 'SSH_KEY_FILE="agent_ed25519"' not in secret_setup:
        die("secret setup must populate the agent_ed25519 key consumed by the Sandbox")
    if "hermes_agent_ed25519" in secret_setup:
        die("secret setup must not retain the retired Hermes SSH data key")
    validate_ssh_secret_migration()

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
