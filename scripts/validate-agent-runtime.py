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
OPERATOR_NAME = "bot-jchristensen"
OPERATOR_PROFILES = (REPO / "examples/personal.yaml", REPO / "examples/work.yaml")


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


def validate_release_named_resources(documents: list[dict], release_name: str) -> dict:
    sandboxes = [document for document in documents if document.get("kind") == "Sandbox"]
    if len(sandboxes) != 1:
        die(f"expected exactly one Sandbox for {release_name}, found {len(sandboxes)}")
    sandbox = sandboxes[0]

    expected_claims = {
        f"data-{release_name}",
        f"gitrepos-{release_name}",
        f"models-{release_name}",
    }
    rendered_claims = {
        document["metadata"]["name"]
        for document in documents
        if document.get("kind") == "PersistentVolumeClaim"
    }
    if rendered_claims != expected_claims:
        die(
            f"PVC names must derive from the {release_name} Helm release: "
            f"expected {sorted(expected_claims)}, got {sorted(rendered_claims)}"
        )

    pod_spec = sandbox["spec"]["podTemplate"]["spec"]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    mounted_claims = {
        volumes[name]["persistentVolumeClaim"]["claimName"]
        for name in ("data", "gitrepos", "models")
    }
    if mounted_claims != expected_claims:
        die(
            f"Sandbox volumes must mount the {release_name} release's PVCs: "
            f"expected {sorted(expected_claims)}, got {sorted(mounted_claims)}"
        )

    container = pod_spec["containers"][0]
    if container["name"] != "agent":
        die(f"the Sandbox app container must be named 'agent', not {release_name}")
    env_secrets = {
        item["secretRef"]["name"]
        for item in container.get("envFrom", [])
        if "secretRef" in item
    }
    if f"{release_name}-secrets" not in env_secrets:
        die(f"Sandbox credentials must use the {release_name}-secrets Secret")
    if volumes["ssh-key"]["secret"]["secretName"] != f"{release_name}-ssh-key":
        die(f"Sandbox SSH identity must use the {release_name}-ssh-key Secret")

    return sandbox


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


def validate_platform_naming_contracts() -> None:
    expected_image = "harbor.hahomelabs.com/vicegerent/agent"
    defaults = yaml.safe_load(
        (REPO / "values.defaults.yaml").read_text(encoding="utf-8")
    )
    if defaults["agentDefaults"]["image"]["repository"] != expected_image:
        die("the default sandbox image repository must use the generic agent identity")
    example = yaml.safe_load(
        (REPO / "values.example.yaml").read_text(encoding="utf-8")
    )
    example_name = example["agents"][0]["name"]
    if example_name in {"agent", "hermes", OPERATOR_NAME}:
        die(
            "values.example.yaml must demonstrate an operator-chosen release name "
            "distinct from platform, upstream, and committed operator identities"
        )

    chart = yaml.safe_load(
        (REPO / "charts/agent/Chart.yaml").read_text(encoding="utf-8")
    )
    if chart.get("name") != "agent":
        die("the sandbox Helm chart must use the generic agent identity")
    if not (REPO / "images/agent").is_dir() or (REPO / "images/hermes").exists():
        die("the derived sandbox image must live only under images/agent")
    image_makefile = (REPO / "images/agent/Makefile").read_text(encoding="utf-8")
    if f"IMAGE := {expected_image}" not in image_makefile:
        die("the agent image Makefile must publish the generic image repository")
    dockerfile = (REPO / "images/agent/Dockerfile").read_text(encoding="utf-8")
    if "FROM nousresearch/hermes-agent:" not in dockerfile:
        die("the generic agent image must retain the upstream Hermes base image")

    stages = yaml.safe_load(
        (REPO / "stages/stages.yaml").read_text(encoding="utf-8")
    )
    agents_stage = next(
        (stage for stage in stages["stages"] if stage["name"] == "agents"), None
    )
    agent_actions = [
        action
        for action in (agents_stage or {}).get("actions", [])
        if action.get("name") == "agent"
    ]
    if len(agent_actions) != 1 or not (
        agent_actions[0].get("type") == "local"
        and agent_actions[0].get("namespace") == "agent-sandbox"
    ):
        die("the agents stage must install the generic agent chart")

    renovate = json.loads((REPO / "renovate.json").read_text(encoding="utf-8"))
    image_rules = [
        rule
        for rule in renovate["packageRules"]
        if expected_image in rule.get("matchPackageNames", [])
    ]
    expected_versioning = (
        "regex:^v?(?<major>\\d+)\\.(?<minor>\\d+)\\.(?<patch>\\d+)"
        "-rev(?<build>\\d+)$"
    )
    if len(image_rules) != 1 or image_rules[0].get("versioning") != expected_versioning:
        die("Renovate must track numeric revN builds of the generic agent image")

    gitlab_ci = (REPO / ".gitlab-ci.yml").read_text(encoding="utf-8")
    if "make -C images/agent release" not in gitlab_ci:
        die("GitLab CI must build the generic agent image directory")

    active_sources = [
        REPO / ".gitlab-ci.yml",
        REPO / "renovate.json",
        REPO / "values.defaults.yaml",
        REPO / "values.example.yaml",
        *(REPO / "examples").glob("*.yaml"),
        *(REPO / "charts").rglob("*"),
        *(REPO / "scripts").rglob("*"),
        *(REPO / "stages").rglob("*"),
        *(REPO / "host").rglob("*"),
        *(REPO / "images/agent").rglob("*"),
    ]
    # Search active implementation/configuration only. Documentation intentionally
    # retains rollback names, while the exact upstream Hermes contracts are not retired.
    retired_identifiers = (
        "images/hermes",
        "vicegerent/hermes-agent",
        "/opt/hermes-ssh",
        "hermes-ssh-key",
        "hermes_agent_ed25519",
        ":-hermes}",
        "HERMES_DASHBOARD_NAMESPACE",
        "HERMES_DASHBOARD_NODEPORT",
        "HERMES_DASHBOARD_SERVICE",
        "HERMES_SKILLS_DIR",
        "HERMES_IMAGE",
    )
    for source in active_sources:
        if not source.is_file() or source == Path(__file__):
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if OPERATOR_NAME in text and source not in OPERATOR_PROFILES:
            die(
                f"active generic source {source.relative_to(REPO)} uses the committed "
                "operator identity"
            )
        for identifier in retired_identifiers:
            if identifier in text:
                die(f"active platform source {source.relative_to(REPO)} uses {identifier}")

    gateway_test = (REPO / "scripts/test-mcp-gateway.sh").read_text(
        encoding="utf-8"
    )
    policy_test = (REPO / "scripts/test-mcp-policies.sh").read_text(encoding="utf-8")
    redaction_placeholder = "".join(("<", "masked", ">"))
    if redaction_placeholder in gateway_test or redaction_placeholder in policy_test:
        die("MCP probes must not contain a literal redaction placeholder")
    if 'API_KEY="${API_KEY:-agent}"' not in gateway_test:
        die("the MCP gateway probe must use the generic agent placeholder token")
    if 'API_KEY="${MY_KEY:-agent}"' not in policy_test:
        die("the MCP policy probe must use the generic agent placeholder token")


def main() -> None:
    validate_platform_naming_contracts()
    baseline_restart_job = render_restart_job_name()
    changed_config_restart_job = render_restart_job_name({"tuning": {"maxTurns": 101}})
    if baseline_restart_job == changed_config_restart_job:
        die("agent config changes must create a new restart Job so the gateway reloads them")

    for profile in OPERATOR_PROFILES:
        configured = yaml.safe_load(profile.read_text(encoding="utf-8"))
        release_name = configured["agents"][0]["name"]
        if release_name != OPERATOR_NAME:
            die(
                f"{profile.relative_to(REPO)} must name the operator's agent "
                f"'{OPERATOR_NAME}'"
            )
        documents = render_documents(values_file=profile, release_name=release_name)
        sandbox = validate_release_named_resources(documents, release_name)
        if sandbox["metadata"]["name"] != OPERATOR_NAME:
            die(f"{profile.relative_to(REPO)} must render the operator's Sandbox")
        pod_template = sandbox["spec"]["podTemplate"]
        if (
            pod_template["metadata"]["labels"].get("vicegerent.io/dashboard")
            != OPERATOR_NAME
        ):
            die(f"{profile.relative_to(REPO)} must label the operator's pod")
        container = pod_template["spec"]["containers"][0]
        if container["name"] != "agent":
            die(f"{profile.relative_to(REPO)} must render the shared 'agent' container name")

    alternate_name = "alternate-agent"
    alternate_documents = render_documents(release_name=alternate_name)
    alternate = validate_release_named_resources(alternate_documents, alternate_name)
    alternate_template = alternate["spec"]["podTemplate"]
    alternate_container = alternate_template["spec"]["containers"][0]
    if not (
        alternate["metadata"]["name"] == alternate_name
        and alternate_template["metadata"]["labels"].get("vicegerent.io/dashboard")
        == alternate_name
        and alternate_container["name"] == "agent"
    ):
        die("Sandbox and pod label identity must follow the Helm release name; container must stay 'agent'")

    log_values = yaml.safe_load(
        (REPO / "stages/values/victoria-logs.yaml").read_text(encoding="utf-8")
    )
    log_scope = log_values["vector"]["customConfig"]["transforms"]["scope"][
        "condition"
    ]["source"]
    required_log_scope = (
        '.kubernetes.pod_labels."vicegerent.io/dashboard"',
        'sandbox_agent != ""',
        'container == "agent"',
    )
    if any(fragment not in log_scope for fragment in required_log_scope):
        die("VictoriaLogs must select every sandbox's shared 'agent' container")

    pod_spec = render_sandbox()["spec"]["podTemplate"]["spec"]
    agent = pod_spec["containers"][0]
    env = {item["name"]: item.get("value") for item in agent["env"]}
    if env.get("OPENCODE_EXPERIMENTAL_LSP_TOOL") != "1":
        die("OpenCode's LSP tool must be enabled in the agent runtime")
    if not (
        env.get("HERMES_HOME") == "/opt/data"
        and env.get("HERMES_DASHBOARD") == "1"
        and env.get("HERMES_DASHBOARD_HOST") == "0.0.0.0"
    ):
        die("the agent runtime must preserve Hermes's upstream environment contract")
    if "-i /opt/agent-ssh/agent_ed25519 " not in env.get("GIT_SSH_COMMAND", ""):
        die("GIT_SSH_COMMAND must use the generic agent SSH key path")

    volume_mounts = {item["name"]: item for item in agent["volumeMounts"]}
    if not (
        volume_mounts["models"]["mountPath"].startswith("/opt/data/.hermes/")
        and volume_mounts["approval-policy"]["mountPath"].startswith("/opt/hermes/")
    ):
        die("the agent runtime must preserve Hermes's upstream filesystem contract")
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
