#!/usr/bin/env python3
"""Assert platform naming/identity contracts (Dockerfile, Renovate, CI, retired-identifier
scan) and load-bearing agent runtime ownership in the rendered Sandbox."""

from __future__ import annotations

import json
import os
import re
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


def validate_slack_dotenv_sync(seed_script: str) -> None:
    start = seed_script.index("slack_credentials_dir=/reload/slack-credentials")
    end = seed_script.index("# Bazel ignores JAVA_TOOL_OPTIONS", start)
    sync_script = seed_script[start:end]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        hermes_home = home / ".hermes"
        hermes_home.mkdir(parents=True)
        dotenv = hermes_home / ".env"
        credentials = root / "credentials"
        credentials.mkdir()
        slack_values = {
            "SLACK_BOT_TOKEN": "bot-token",
            "SLACK_APP_TOKEN": "app-token",
            "SLACK_ALLOWED_USERS": "U0123456789",
            "SLACK_HOME_CHANNEL": "D0123456789",  # pragma: allowlist secret
        }
        for name, value in slack_values.items():
            (credentials / name).write_text(value, encoding="utf-8")

        # The init container runs under `set -euo pipefail`; without it here a
        # regression that trips errexit in the real Sandbox would pass.
        executable = "set -euo pipefail\n" + sync_script.replace(
            "/reload/slack-credentials", str(credentials)
        )

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", executable],
                env={"HOME": str(home), "PATH": os.environ["PATH"]},
                capture_output=True,
                text=True,
            )

        managed = "".join(f"{name}={value}\n" for name, value in slack_values.items())

        result = run()
        if result.returncode != 0:
            die(f"Slack dotenv synchronization on a fresh home failed: {result.stderr.strip()}")
        if dotenv.read_text(encoding="utf-8") != managed:
            die("Slack dotenv synchronization must create Hermes .env when none exists")

        # `export`-prefixed assignments are the other spelling Hermes' loader accepts.
        dotenv.write_text(
            "TERMINAL_TIMEOUT=120\nSLACK_BOT_TOKEN=old\nexport SLACK_HOME_CHANNEL=old\n",
            encoding="utf-8",
        )
        result = run()
        if result.returncode != 0:
            die(f"Slack dotenv synchronization failed: {result.stderr.strip()}")
        if dotenv.read_text(encoding="utf-8") != "TERMINAL_TIMEOUT=120\n" + managed:
            die("Slack dotenv synchronization must preserve non-Slack entries and replace managed Slack keys")
        if dotenv.stat().st_mode & 0o777 != 0o600:
            die("Slack dotenv synchronization must keep Hermes .env private")

        for credential in credentials.iterdir():
            credential.unlink()
        result = run()
        if result.returncode != 0:
            die(f"Slack dotenv cleanup failed: {result.stderr.strip()}")
        if dotenv.read_text(encoding="utf-8") != "TERMINAL_TIMEOUT=120\n":
            die("Slack dotenv synchronization must remove stale Slack credentials when Slack is disabled")

        # An unreadable dotenv makes grep exit 2; that must abort rather than
        # replace Hermes .env with the empty output. Root can read it regardless.
        if os.geteuid() != 0:
            dotenv.chmod(0o000)
            result = run()
            dotenv.chmod(0o600)
            if result.returncode == 0:
                die("a dotenv read failure must abort the Slack sync")
            if dotenv.read_text(encoding="utf-8") != "TERMINAL_TIMEOUT=120\n":
                die("a dotenv read failure must not truncate Hermes .env")

        (credentials / "SLACK_BOT_TOKEN").write_text("partial-token", encoding="utf-8")
        result = run()
        if result.returncode == 0:
            die("Slack dotenv synchronization must reject incomplete Slack credentials")
        # By name, so a downstream crash on the missing file cannot pass for a rejection.
        if "Slack is configured but SLACK_APP_TOKEN is missing" not in result.stderr:
            die("incomplete Slack credentials must be rejected by the explicit guard: " + result.stderr.strip())
        if dotenv.read_text(encoding="utf-8") != "TERMINAL_TIMEOUT=120\n":
            die("incomplete Slack credentials must not alter Hermes .env")


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
    end_marker = "        end' \\\n  | kubectl --context \"$KUBE_CONTEXT\" create -f -"
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
    cache_contracts = (
        "CACHE_REF ?= $(IMAGE):buildcache",
        "--cache-from=type=registry,ref=$(CACHE_REF)",
        "--cache-to=type=registry,ref=$(CACHE_REF),mode=max",
    )
    missing_cache_contracts = [
        item for item in cache_contracts if item not in image_makefile
    ]
    if missing_cache_contracts:
        die(
            "the agent image build must preserve all stages in its registry cache: "
            + ", ".join(missing_cache_contracts)
        )
    dockerfile = (REPO / "images/agent/Dockerfile").read_text(encoding="utf-8")
    dhi_base = re.search(
        r"^FROM dhi\.io/python:\d+\.\d+-debian\d+-dev@sha256:[0-9a-f]{64} AS sandbox-base$",
        dockerfile,
        re.MULTILINE,
    )
    if dhi_base is None:
        die(
            "the generic agent image sandbox-base stage must use a tagged and "
            "digest-pinned DHI base"
        )
    if re.search(r"^FROM sandbox-base AS runtime$", dockerfile, re.MULTILINE) is None:
        die(
            "the Hermes-specific runtime stage must build FROM the generic "
            "sandbox-base checkpoint"
        )
    runtime_packages = dockerfile[
        dhi_base.end() : dockerfile.index("COPY --from=sqlite_build", dhi_base.end())
    ]
    if re.search(r"^\s+gzip \\\s*$", runtime_packages, re.MULTILINE) is None:
        die("the DHI runtime package layer must install gzip for tar.gz extraction")
    runtime_stage = dockerfile[dhi_base.end() :]
    apt_layers = re.findall(r"^RUN apt-get(?:\s|$)", runtime_stage, re.MULTILINE)
    if len(apt_layers) != 1:
        die("the DHI runtime must keep all directly managed OS packages in one apt layer")
    hermes_sha_install = (
        "COPY --link --chmod=a+rX,go-w --from=hermes_source /src/ .\n"
        "ARG HERMES_GIT_SHA\n"
        'RUN uv pip install --no-cache-dir --no-deps -e "."'
    )
    if (
        runtime_stage.count("\nARG HERMES_GIT_SHA\n") != 1
        or hermes_sha_install not in runtime_stage
    ):
        die(
            "the runtime HERMES_GIT_SHA argument must be declared only at its "
            "first use so Hermes updates preserve the stable runtime cache prefix"
        )
    shim_first_path = (
        'PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:${PATH}"'
    )
    if dockerfile.count(shim_first_path) != 1:
        die("the runtime PATH must put the Hermes privilege-drop shim before the venv")
    if 'ENV PATH="/opt/hermes/.venv/bin:${PATH}"' in dockerfile:
        die("a later Docker ENV must not move the Hermes venv ahead of its exec shim")
    profile_path = 'export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:$PATH"'
    if profile_path not in dockerfile:
        die("login-shell PATH fallback must preserve exec-shim precedence")
    if "FROM nousresearch/hermes-agent:" in dockerfile:
        die("the generic agent image must not inherit the prebuilt Hermes image")
    if re.search(
        r"^WORKDIR /workspace\n\nENTRYPOINT \[ \"/opt/hermes/docker/entrypoint-dispatch\.sh\" \]$",
        dockerfile,
        re.MULTILINE,
    ) is None:
        die("the final agent image working directory must be /workspace")
    if re.search(r"^RUN groupadd --gid 10000 agent \\$", dockerfile, re.MULTILINE) is None:
        die("the generic agent image must create the neutral agent group at gid 10000")
    if re.search(
        r"^\s*&& useradd --uid 10000 --gid 10000 --create-home "
        r"--home-dir /opt/data --shell /bin/sh agent \\$",
        dockerfile,
        re.MULTILINE,
    ) is None:
        die("the generic agent image must create the neutral agent user at uid 10000")
    if re.search(r"\b(?:useradd|groupadd)\b[^\n]*\bhermes\b", dockerfile):
        die("the generic agent image must not create a Hermes-named Linux account")
    if re.search(r"^ARG HERMES_VERSION=v\d+\.\d+\.\d+$", dockerfile, re.MULTILINE) is None:
        die("the agent image must pin the upstream Hermes release tag")
    if re.search(r"^ARG HERMES_GIT_SHA=[0-9a-f]{40}$", dockerfile, re.MULTILINE) is None:
        die("the agent image must pin the upstream Hermes release commit")
    source_contracts = (
        '--branch "${HERMES_VERSION}"',
        'test "$(git -C /src rev-parse HEAD)" = "${HERMES_GIT_SHA}"',
        "/opt/sqlite-fixed/bin/sqlite3",
        "&& sqlite3 -version",
        "/src/docker/tini-shim.sh /usr/bin/tini",
        "COPY --link --chmod=a+rX,go-w --from=hermes_source /src/ .",
        'org.opencontainers.image.base.name="dhi.io/python:',
        "ENV HOME=/opt/data",
        "HERMES_HOME=/opt/data/.hermes",
        "HERMES_LAZY_INSTALL_TARGET=/opt/data/.hermes/lazy-packages",
        "COPY home-scripts/migrate-hermes-home.sh /usr/local/bin/migrate-hermes-home",
        "cp -a /opt/hermes/docker/hermes-exec-shim.sh /opt/hermes/bin/hermes",
        'ENTRYPOINT [ "/opt/hermes/docker/entrypoint-dispatch.sh" ]',
    )
    missing_contracts = [item for item in source_contracts if item not in dockerfile]
    if missing_contracts:
        die(
            "the DHI source build is missing required Hermes contracts: "
            + ", ".join(missing_contracts)
        )

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
    for startup_override in (
        {"soul": "changed prompt"},
        {"harnesses": {"claudeCode": "claude-haiku-4-5"}},
        {"config": {"approvals": {"mode": "auto"}}},
    ):
        if baseline_restart_job == render_restart_job_name(startup_override):
            die("every startup-consumed prompt, harness, approval, and Sandbox payload must create a new restart Job")

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
        env.get("HERMES_HOME") == "/opt/data/.hermes"
        and env.get("HERMES_DASHBOARD") == "1"
        and env.get("HERMES_DASHBOARD_HOST") == "0.0.0.0"
        and env.get("TERMINAL_HOME_MODE") == "real"
    ):
        die("the agent runtime must isolate Hermes state while preserving its environment contract")
    if "-i /opt/agent-ssh/agent_ed25519 " not in env.get("GIT_SSH_COMMAND", ""):
        die("GIT_SSH_COMMAND must use the generic agent SSH key path")

    volume_mounts = {item["name"]: item for item in agent["volumeMounts"]}
    if not (
        volume_mounts["models"]["mountPath"].startswith("/opt/data/.hermes/")
        and volume_mounts["approval-policy"]["mountPath"].startswith("/opt/hermes/")
        and volume_mounts["soul"]["mountPath"] == "/opt/data/.hermes/SOUL.md"
    ):
        die("the agent runtime must isolate Hermes state while preserving its install tree")
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

    seed_data = next(
        container
        for container in pod_spec["initContainers"]
        if container["name"] == "seed-data"
    )
    seed_script = seed_data["args"][0]
    required_home_layout = (
        "/usr/local/bin/migrate-hermes-home",
        'fastembed_dest="/opt/data/.hermes/cache/fastembed"',
        "/opt/data/.hermes/plugins/mnemosyne",
        "reconcile_config hermes yaml /opt/data/.hermes/config.yaml",
        "touch /opt/data/.hermes/.restart_pending.json",
    )
    missing_layout = [item for item in required_home_layout if item not in seed_script]
    if missing_layout:
        die("seed-data is missing the split Hermes-home contract: " + ", ".join(missing_layout))

    slack_keys = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS", "SLACK_HOME_CHANNEL"]
    slack_volume = volumes.get("slack-credentials", {}).get("secret", {})
    if slack_volume != {
        "secretName": "agent-secrets",  # pragma: allowlist secret
        "defaultMode": 0o440,
        "optional": True,
        "items": [{"key": key, "path": key} for key in slack_keys],
    }:
        die("seed-data must receive only the Slack keys of the release-named Secret, never password/signing-secret")
    slack_mount = next(
        (
            mount
            for mount in seed_data.get("volumeMounts", [])
            if mount.get("name") == "slack-credentials"
        ),
        None,
    )
    if slack_mount != {
        "name": "slack-credentials",
        "mountPath": "/reload/slack-credentials",
        "readOnly": True,
    }:
        die("seed-data must mount Slack credentials read-only outside the Hermes home")
    required_slack_sync = (
        "slack_credentials_dir=/reload/slack-credentials",
        'slack_dotenv="${HOME}/.hermes/.env"',
        f'slack_names="{" ".join(slack_keys)}"',
        "mv \"${slack_dotenv_tmp}\" \"${slack_dotenv}\"",
        "chmod 600 \"${slack_dotenv}\"",
    )
    missing_slack_sync = [item for item in required_slack_sync if item not in seed_script]
    if missing_slack_sync:
        die("seed-data must atomically synchronize Slack credentials into Hermes .env: " + ", ".join(missing_slack_sync))
    validate_slack_dotenv_sync(seed_script)

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
