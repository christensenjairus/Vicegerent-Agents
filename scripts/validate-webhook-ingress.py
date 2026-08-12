#!/usr/bin/env python3
"""Render-test the in-cluster webhook listener and its agent boundary."""
from __future__ import annotations

import copy
import json
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROOT / "values.defaults.yaml").read_text(encoding="utf-8"))
EXAMPLE = yaml.safe_load((ROOT / "values.example.yaml").read_text(encoding="utf-8"))
SECRET_PATTERNS = ROOT / "images" / "mcp-cerbos-shim" / "internal" / "server" / "secret-patterns.json"
PROMPT_INJECTION_PATTERNS = ROOT / "images" / "mcp-cerbos-shim" / "internal" / "promptinjection" / "patterns.json"


def render(chart: str, values: list[dict], *, expect_failure: bool = False) -> tuple[list[dict], str]:
    with tempfile.TemporaryDirectory() as temporary:
        command = [
            "helm",
            "template",
            "ops" if chart == "agent" else chart,
            str(ROOT / "charts" / chart),
            "--namespace",
            {
                "webhook-listener": "webhooks",
                "egress-proxy": "egress-proxy",
            }.get(chart, "agent-sandbox"),
        ]
        for index, value in enumerate(values):
            path = Path(temporary) / f"values-{index}.yaml"
            path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            command.extend(["-f", str(path)])
        if chart == "egress-proxy":
            command.extend(["--set-file", f"secretPatterns={SECRET_PATTERNS}"])
            command.extend(["--set-file", f"promptInjectionPatterns={PROMPT_INJECTION_PATTERNS}"])
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)

    output = result.stdout + result.stderr
    if expect_failure:
        if result.returncode == 0:
            raise AssertionError(f"{chart} unexpectedly rendered invalid values")
        return [], output
    if result.returncode != 0:
        raise AssertionError(f"{chart} render failed:\n{output}")
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    return documents, result.stdout


def document(documents: list[dict], kind: str, name: str) -> dict:
    matches = [
        item
        for item in documents
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"wanted one {kind}/{name}, found {len(matches)}")
    return matches[0]


def machine_values() -> dict:
    agent = copy.deepcopy(EXAMPLE["agents"][0])
    agent["name"] = "ops"
    agent["webhooks"]["enabled"] = True
    agent["webhooks"]["routes"]["github-pushes"] = {
        "provider": "github",
        "description": "Investigate protected-branch pushes",
        "events": ["push"],
        "prompt": "Push to {ref}",
        "skills": ["production-alert-auditing"],
        "filters": {
            "all": [
                {"field": "ref", "equals": "refs/heads/main"},
                {"field": "event", "equals": "push"},
            ]
        },
        "script": "normalize-push.py",
        "deliver": "slack",
        "deliver_extra": {"chat_id": "D0123456789"},
        "deliver_only": False,
    }
    agent["webhooks"]["routes"]["alertmanager-alerts"] = {
        "provider": "alertmanager",
        "prompt": "Alert {commonLabels.alertname}",
    }
    agent["webhooks"]["routes"]["disabled-route"] = {
        "enabled": False,
        "provider": "gitlab",
    }
    return {
        "webhooks": {
            "publicUrl": "https://hooks.example.com",
            "tunnelId": "6ff42ae2-765d-4adf-8112-31c55c1551ef",
        },
        "agents": [agent],
    }


def render_agent(machine: dict, *, expect_failure: bool = False) -> tuple[list[dict], str]:
    return render(
        "agent",
        [DEFAULTS["agentDefaults"], machine["agents"][0]],
        expect_failure=expect_failure,
    )


def assert_disabled() -> None:
    machine = machine_values()
    machine["agents"][0]["webhooks"]["enabled"] = False
    listener_documents, _ = render("webhook-listener", [DEFAULTS, machine])
    if listener_documents:
        raise AssertionError("disabled webhook listener rendered resources")
    proxy_documents, _ = render("egress-proxy", [DEFAULTS, machine])
    proxy_names = {item.get("metadata", {}).get("name") for item in proxy_documents}
    if "webhook-egress-proxy" in proxy_names:
        raise AssertionError("disabled webhooks rendered the dedicated proxy")

    agent_documents, output = render_agent(machine)
    forbidden = {"ops-webhook", "ops-webhook-ingress"}
    rendered_names = {item.get("metadata", {}).get("name") for item in agent_documents}
    if forbidden & rendered_names or "containerPort: 8644" in output:
        raise AssertionError("disabled agent rendered webhook network resources")
    config_map = document(agent_documents, "ConfigMap", "ops-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    if "webhook" in config.get("platforms", {}):
        raise AssertionError("disabled agent rendered a Hermes webhook platform")


def assert_invalid_values() -> None:
    for invalid_url in (
        "http://hooks.example.com",
        "https://hooks.example.com/",
        "https://hooks.example.com/path",
        "https://hooks.example.com?query=yes",
        "https://user@hooks.example.com",
        "https://hooks.example.com:443",
        "https://Hooks.Example.com",
    ):
        machine = machine_values()
        machine["webhooks"]["publicUrl"] = invalid_url
        _, output = render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        if "webhooks.publicUrl" not in output:
            raise AssertionError(f"invalid public URL produced an unclear error: {invalid_url}")

    # The all-zero UUID is the shipped placeholder, so it has to be rejected by
    # shape-independent means: it passes the UUID pattern but names no tunnel.
    for invalid_tunnel in (
        "",
        "not-a-uuid",
        "6FF42AE2-765D-4ADF-8112-31C55C1551EF",
        "00000000-0000-0000-0000-000000000000",
    ):
        machine = machine_values()
        machine["webhooks"]["tunnelId"] = invalid_tunnel
        _, output = render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        if "webhooks.tunnelId" not in output:
            raise AssertionError(f"invalid tunnel ID produced an unclear error: {invalid_tunnel!r}")

    invalid_routes = {
        "non-map route": "not-a-map",
        "bad route name": {"provider": "github"},
    }
    for label, route in invalid_routes.items():
        machine = machine_values()
        routes = machine["agents"][0]["webhooks"]["routes"]
        routes.clear()
        routes["Bad_Route" if label == "bad route name" else "invalid"] = route
        render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        render_agent(machine, expect_failure=True)

    machine = machine_values()
    machine["agents"][0]["webhooks"]["enabled"] = "yes"
    render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
    render_agent(machine, expect_failure=True)

    for invalid_toolsets in ("terminal", ["terminal", 1]):
        machine = machine_values()
        machine["agents"][0]["webhooks"]["toolsets"] = invalid_toolsets
        render_agent(machine, expect_failure=True)

    machine = machine_values()
    machine["agents"][0]["webhooks"]["routes"]["pagerduty-incidents"]["enabled"] = "yes"
    render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
    render_agent(machine, expect_failure=True)

    for alias in ("generic", "generic-v1", "github-hmac", "pagerduty-v3", "svix-hmac", "alert-manager", "prometheus"):
        machine = machine_values()
        machine["agents"][0]["webhooks"]["routes"]["pagerduty-incidents"]["provider"] = alias
        render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        render_agent(machine, expect_failure=True)

    machine = machine_values()
    machine["agents"][0]["webhooks"]["routes"]["pagerduty-incidents"]["respond"] = True
    _, output = render_agent(machine, expect_failure=True)
    if "respond is unsupported; webhook routes are asynchronous" not in output:
        raise AssertionError("respond rejection produced an unclear error")

    for field in (
        "secretRef",
        "secret",
        "secret_env",
        "secretFile",
        "targetURL",
        "trusted_proxy",
        "signature_provider",
    ):
        machine = machine_values()
        machine["agents"][0]["webhooks"]["routes"]["pagerduty-incidents"][field] = "forbidden"
        render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        render_agent(machine, expect_failure=True)

    invalid_route_fields = {
        "description": [],
        "events": "incident.triggered",
        "prompt": [],
        "skills": "production-alert-auditing",
        "filters": "severity=critical",
        "script": [],
        "deliver": False,
        "deliver_extra": [],
        "deliver_only": "yes",
        "unknown": True,
    }
    for field, value in invalid_route_fields.items():
        machine = machine_values()
        machine["agents"][0]["webhooks"]["routes"]["pagerduty-incidents"][field] = value
        render_agent(machine, expect_failure=True)

    machine = machine_values()
    route = machine["agents"][0]["webhooks"]["routes"]["pagerduty-incidents"]
    route["deliver_only"] = True
    route.pop("deliver", None)
    _, output = render_agent(machine, expect_failure=True)
    if "deliver_only requires a non-log delivery target" not in output:
        raise AssertionError("deliver_only rejection produced an unclear error")

    for routes in (
        {},
        {"off": {"enabled": False, "provider": "github"}},
    ):
        machine = machine_values()
        machine["agents"][0]["webhooks"]["routes"] = routes
        render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        render_agent(machine, expect_failure=True)


def assert_agent_boundary(machine: dict) -> None:
    documents, _ = render_agent(machine)

    config_map = document(documents, "ConfigMap", "ops-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    webhook = config["platforms"]["webhook"]
    if webhook["extra"]["host"] != "0.0.0.0" or webhook["extra"]["port"] != 8644:
        raise AssertionError("Hermes webhook listener is not pinned to port 8644")
    for route in webhook["extra"]["routes"].values():
        if route.get("trusted_proxy") is not True:
            raise AssertionError("Hermes route is missing trusted_proxy")
        forbidden = {"provider", "secretRef", "secret", "secret_env", "signature_provider"}
        if forbidden & route.keys():
            raise AssertionError(f"Hermes route contains signing configuration: {route}")
    expected_toolsets = DEFAULTS["agentDefaults"]["webhooks"]["toolsets"]
    if config.get("platform_toolsets", {}).get("webhook") != expected_toolsets:
        raise AssertionError("webhook agent did not receive the default toolset allowlist")

    github_route = webhook["extra"]["routes"]["github-pushes"]
    expected_route = {
        "description": "Investigate protected-branch pushes",
        "events": ["push"],
        "prompt": "Push to {ref}",
        "skills": ["production-alert-auditing"],
        "filters": {
            "all": [
                {"field": "ref", "equals": "refs/heads/main"},
                {"field": "event", "equals": "push"},
            ]
        },
        "script": "normalize-push.py",
        "deliver": "slack",
        "deliver_extra": {"chat_id": "D0123456789"},
        "deliver_only": False,
        "trusted_proxy": True,
    }
    if github_route != expected_route:
        raise AssertionError(f"Hermes route options were not preserved: {github_route}")

    service = document(documents, "Service", "ops-webhook")
    if service["spec"].get("type") != "ClusterIP":
        raise AssertionError("agent webhook Service is not ClusterIP-only")
    if service["spec"]["ports"] != [{"name": "webhook", "port": 8644, "targetPort": "webhook"}]:
        raise AssertionError("agent webhook Service exposes unexpected ports")

    policy = document(documents, "CiliumNetworkPolicy", "ops-webhook-ingress")
    expected_ingress = [
        {
            "fromEndpoints": [
                {
                    "matchLabels": {
                        "io.kubernetes.pod.namespace": "egress-proxy",
                        "app.kubernetes.io/name": "webhook-egress-proxy",
                    }
                }
            ],
            "toPorts": [
                {
                    "ports": [{"port": "8644", "protocol": "TCP"}],
                    "rules": {
                        "http": [
                            {"method": "POST", "path": "^/webhooks/alertmanager-alerts$"},
                            {"method": "POST", "path": "^/webhooks/github-pushes$"},
                            {"method": "POST", "path": "^/webhooks/pagerduty-incidents$"},
                        ]
                    },
                }
            ],
        }
    ]
    if policy["spec"].get("ingress") != expected_ingress:
        raise AssertionError("agent ingress is not restricted to its configured proxy routes")


def assert_webhook_toolset_override(machine: dict) -> None:
    restricted = copy.deepcopy(machine)
    restricted["agents"][0]["webhooks"]["toolsets"] = ["web"]
    documents, _ = render_agent(restricted)
    config_map = document(documents, "ConfigMap", "ops-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    if config.get("platform_toolsets", {}).get("webhook") != ["web", "no_mcp"]:
        raise AssertionError("webhook toolset override did not replace defaults or disable implicit MCP access")

    no_tools = copy.deepcopy(machine)
    no_tools["agents"][0]["webhooks"]["toolsets"] = []
    documents, _ = render_agent(no_tools)
    config_map = document(documents, "ConfigMap", "ops-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    if config.get("platform_toolsets", {}).get("webhook") != ["no_mcp"]:
        raise AssertionError("empty webhook toolset override did not disable all tools")


def assert_multi_agent_routing() -> None:
    machine = machine_values()
    second = copy.deepcopy(machine["agents"][0])
    second["name"] = "security"
    second["webhooks"]["routes"] = {
        "gitlab-events": {
            "provider": "gitlab",
            "events": ["Merge Request Hook"],
            "prompt": "Merge request {object_attributes.title}",
        }
    }
    machine["agents"].append(second)

    documents, _ = render("webhook-listener", [DEFAULTS, machine])
    config_map = document(documents, "ConfigMap", "webhook-listener")
    routes = json.loads(config_map["data"]["routes.json"])["routes"]
    expected_targets = {
        ("ops", "pagerduty-incidents"): "http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/pagerduty-incidents",
        ("ops", "github-pushes"): "http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/github-pushes",
        ("ops", "alertmanager-alerts"): "http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/alertmanager-alerts",
        ("security", "gitlab-events"): "http://security-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/gitlab-events",
    }
    actual_targets = {
        (route["agent"], route["route"]): route["targetURL"] for route in routes
    }
    if actual_targets != expected_targets:
        raise AssertionError(f"shared listener cross-targeted an agent route: {routes}")


def assert_listener(machine: dict) -> None:
    documents, output = render("webhook-listener", [DEFAULTS, machine])
    if len(documents) != 7:
        raise AssertionError(f"listener rendered {len(documents)} resources instead of 7")
    if "ngrok" in output.lower():
        raise AssertionError("listener render still references ngrok")

    config_map = document(documents, "ConfigMap", "webhook-listener")
    routes = json.loads(config_map["data"]["routes.json"])["routes"]
    expected_routes = {
        ("ops", "pagerduty-incidents", "pagerduty", "ops__pagerduty-incidents"),
        ("ops", "github-pushes", "github", "ops__github-pushes"),
        ("ops", "alertmanager-alerts", "alertmanager", "ops__alertmanager-alerts"),
    }
    actual_routes = {
        (route["agent"], route["route"], route["provider"], route["secretFile"])
        for route in routes
    }
    if actual_routes != expected_routes or "disabled-route" in output:
        raise AssertionError(f"listener routing table is wrong: {routes}")
    for route in routes:
        expected = f"http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/{route['route']}"
        if route["targetURL"] != expected:
            raise AssertionError(f"listener target escaped its agent boundary: {route}")

    tunnel_config_map = document(documents, "ConfigMap", "cloudflared")
    tunnel_config = yaml.safe_load(tunnel_config_map["data"]["config.yaml"])
    if tunnel_config["tunnel"] != machine["webhooks"]["tunnelId"]:
        raise AssertionError("cloudflared is not pinned to the configured tunnel")
    if tunnel_config["credentials-file"] != "/etc/cloudflared/creds/credentials.json":
        raise AssertionError("cloudflared credentials path drifted")
    if tunnel_config.get("no-autoupdate") is not True:
        raise AssertionError("cloudflared auto-update is not disabled")
    expected_host = machine["webhooks"]["publicUrl"].removeprefix("https://")
    if tunnel_config["ingress"] != [
        {
            "hostname": expected_host,
            "service": "http://webhook-listener.webhooks.svc.cluster.local:8081",
        },
        {"service": "http_status:404"},
    ]:
        raise AssertionError(f"cloudflared ingress mapping drifted: {tunnel_config['ingress']}")

    service = document(documents, "Service", "webhook-listener")
    if service["spec"].get("type") != "ClusterIP":
        raise AssertionError("listener Service is not ClusterIP-only")
    if service["spec"].get("selector") != {"app.kubernetes.io/name": "webhook-listener"}:
        raise AssertionError("listener Service selector escaped the listener workload")
    if service["spec"].get("ports") != [{"name": "http", "port": 8081, "targetPort": "http"}]:
        raise AssertionError("listener Service exposes unexpected ports")

    listener_deployment = document(documents, "Deployment", "webhook-listener")
    cloudflared_deployment = document(documents, "Deployment", "cloudflared")
    for name, deployment in (
        ("webhook-listener", listener_deployment),
        ("cloudflared", cloudflared_deployment),
    ):
        if deployment["spec"].get("replicas") != 1 or deployment["spec"].get("strategy") != {"type": "Recreate"}:
            raise AssertionError(f"{name} rollouts must replace the single replica, never overlap it")
        expected_selector = {"app.kubernetes.io/name": name}
        if deployment["spec"].get("selector", {}).get("matchLabels") != expected_selector:
            raise AssertionError(f"{name} Deployment selector drifted")
        if deployment["spec"]["template"].get("metadata", {}).get("labels") != expected_selector:
            raise AssertionError(f"{name} pod labels drifted")
        pod = deployment["spec"]["template"]["spec"]
        if pod.get("automountServiceAccountToken") is not False:
            raise AssertionError(f"{name} mounts a service-account token")
        if pod.get("dnsConfig", {}).get("options") != [{"name": "ndots", "value": "1"}]:
            raise AssertionError(f"{name} DNS is not pinned for exact FQDN policy matches")

    listener_pod = listener_deployment["spec"]["template"]["spec"]
    listener_containers = {container["name"]: container for container in listener_pod["containers"]}
    if set(listener_containers) != {"webhook-listener"}:
        raise AssertionError(f"listener pod has unexpected containers: {sorted(listener_containers)}")

    listener = listener_containers["webhook-listener"]
    if listener.get("resources", {}).get("requests") != {"cpu": "25m", "memory": "32Mi"}:
        raise AssertionError("listener has no scheduler resource floor")
    environment = {entry["name"]: entry for entry in listener["env"]}
    if environment["WEBHOOK_LISTEN_ADDRESS"].get("value") != "0.0.0.0:8081":
        raise AssertionError("listener bind does not accept traffic from the cloudflared workload")

    listener_volumes = {volume["name"]: volume for volume in listener_pod["volumes"]}
    if set(listener_volumes) != {"config", "route-secrets"}:
        raise AssertionError("listener pod can access cloudflared configuration or credentials")
    secret_volume = listener_volumes["route-secrets"]
    if secret_volume.get("secret") != {"defaultMode": 256, "secretName": "vicegerent-webhook-secrets"}:  # pragma: allowlist secret
        raise AssertionError("listener does not mount the shared webhook Secret")
    listener_reload = listener_deployment["metadata"].get("annotations", {}).get(
        "secret.reloader.stakater.com/reload"
    )
    if listener_reload != "vicegerent-webhook-secrets":  # pragma: allowlist secret
        raise AssertionError("listener reload annotation includes unrelated Secrets")

    cloudflared_pod = cloudflared_deployment["spec"]["template"]["spec"]
    cloudflared_containers = {container["name"]: container for container in cloudflared_pod["containers"]}
    if set(cloudflared_containers) != {"cloudflared"}:
        raise AssertionError(f"cloudflared pod has unexpected containers: {sorted(cloudflared_containers)}")
    cloudflared = cloudflared_containers["cloudflared"]
    image = cloudflared["image"]
    tag = image.rpartition(":")[2]
    if not image.startswith("docker.io/cloudflare/cloudflared:") or not re.fullmatch(r"\d{4}\.\d+\.\d+", tag):
        raise AssertionError(f"cloudflared image reference is not pinned: {image}")
    if cloudflared.get("args") != ["tunnel", "--config", "/etc/cloudflared/config/config.yaml", "run"]:
        raise AssertionError(f"cloudflared arguments drifted: {cloudflared.get('args')}")
    tunnel_environment = {entry["name"]: entry.get("value") for entry in cloudflared["env"]}
    if tunnel_environment.get("TUNNEL_METRICS") != "127.0.0.1:2000":
        raise AssertionError("cloudflared metrics endpoint is not loopback-only")
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        if cloudflared.get(probe, {}).get("exec", {}).get("command") != ["cloudflared", "tunnel", "ready"]:
            raise AssertionError(f"cloudflared {probe} does not use the built-in readiness command")
    if cloudflared.get("securityContext") != {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }:
        raise AssertionError("cloudflared is not hardened like the listener")
    if any(not mount.get("readOnly") for mount in cloudflared.get("volumeMounts", [])):
        raise AssertionError("cloudflared mounts a writable volume")

    for name, container in {**listener_containers, **cloudflared_containers}.items():
        for entry in container.get("env", []):
            reference = entry.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
            if reference:
                raise AssertionError(f"{name} reads a Secret through the environment: {reference}")

    cloudflared_volumes = {volume["name"]: volume for volume in cloudflared_pod["volumes"]}
    if set(cloudflared_volumes) != {"cloudflared-config", "cloudflared-credentials"}:
        raise AssertionError("cloudflared pod can access listener configuration or signing Secrets")
    credentials_volume = cloudflared_volumes["cloudflared-credentials"]
    if credentials_volume.get("secret") != {"defaultMode": 256, "secretName": "vicegerent-cloudflared-credentials"}:  # pragma: allowlist secret
        raise AssertionError("cloudflared does not mount the tunnel credentials Secret")
    cloudflared_reload = cloudflared_deployment["metadata"].get("annotations", {}).get(
        "secret.reloader.stakater.com/reload"
    )
    if cloudflared_reload != "vicegerent-cloudflared-credentials":  # pragma: allowlist secret
        raise AssertionError("cloudflared reload annotation includes unrelated Secrets")

    listener_policy = document(documents, "CiliumNetworkPolicy", "webhook-listener")
    if listener_policy["spec"].get("endpointSelector") != {
        "matchLabels": {"app.kubernetes.io/name": "webhook-listener"}
    }:
        raise AssertionError("listener policy selects the wrong workload")
    expected_listener_ingress = [{
        "fromEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "webhooks", "app.kubernetes.io/name": "cloudflared"}}],
        "toPorts": [{"ports": [{"port": "8081", "protocol": "TCP"}]}],
    }]
    if listener_policy["spec"].get("ingress") != expected_listener_ingress:
        raise AssertionError("listener ingress is not limited to cloudflared")
    if len(listener_policy["spec"].get("egress", [])) != 2:
        raise AssertionError("listener policy has unexpected egress")
    dns, proxy = listener_policy["spec"]["egress"]
    dns_names = {
        entry["matchName"]
        for entry in dns["toPorts"][0]["rules"]["dns"]
    }
    if dns["toEndpoints"] != [{"matchLabels": {"io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}]:
        raise AssertionError("listener DNS egress is not limited to kube-dns")
    if dns_names != {"webhook-egress-proxy.egress-proxy.svc.cluster.local"}:
        raise AssertionError(f"listener DNS names are too broad: {dns_names}")
    if "toFQDNs" in proxy:
        raise AssertionError("listener can connect to Cloudflare edge hosts")
    if proxy != {
        "toEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "egress-proxy", "app.kubernetes.io/name": "webhook-egress-proxy"}}],
        "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
    }:
        raise AssertionError(f"listener proxy egress is too broad: {proxy}")

    cloudflared_policy = document(documents, "CiliumNetworkPolicy", "cloudflared")
    if cloudflared_policy["spec"].get("endpointSelector") != {
        "matchLabels": {"app.kubernetes.io/name": "cloudflared"}
    }:
        raise AssertionError("cloudflared policy selects the wrong workload")
    if cloudflared_policy["spec"].get("ingress") or len(cloudflared_policy["spec"].get("egress", [])) != 3:
        raise AssertionError("cloudflared policy has an unexpected network direction")
    # cloudflared accepts nothing from the cluster, and Cilium only enforces a
    # direction a policy carries rules for, so the deny has to be explicit.
    if cloudflared_policy["spec"].get("enableDefaultDeny", {}).get("ingress") is not True:
        raise AssertionError("cloudflared ingress is not default-denied")
    cloudflared_dns, tunnel, origin = cloudflared_policy["spec"]["egress"]
    cloudflared_dns_names = {
        entry["matchName"]
        for entry in cloudflared_dns["toPorts"][0]["rules"]["dns"]
    }
    if cloudflared_dns_names != {
        "_v2-origintunneld._tcp.argotunnel.com",
        "region1.v2.argotunnel.com",
        "region2.v2.argotunnel.com",
        "protocol-v2.argotunnel.com",
        "cfd-features.argotunnel.com",
        "webhook-listener.webhooks.svc.cluster.local",
    }:
        raise AssertionError(f"cloudflared DNS names are too broad: {cloudflared_dns_names}")
    # cloudflared negotiates its own edge transport, so both protocols stay open:
    # QUIC over UDP is what it picks, HTTP/2 over TCP is the fallback.
    if tunnel != {
        "toFQDNs": [
            {"matchName": "region1.v2.argotunnel.com"},
            {"matchName": "region2.v2.argotunnel.com"},
        ],
        "toPorts": [{"ports": [{"port": "7844", "protocol": "UDP"}, {"port": "7844", "protocol": "TCP"}]}],
    }:
        raise AssertionError(f"cloudflared tunnel egress does not cover both edge transports: {tunnel}")
    if origin != {
        "toEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "webhooks", "app.kubernetes.io/name": "webhook-listener"}}],
        "toPorts": [{"ports": [{"port": "8081", "protocol": "TCP"}]}],
    }:
        raise AssertionError(f"cloudflared listener egress is too broad: {origin}")
    if "webhook-egress-proxy.egress-proxy.svc.cluster.local" in cloudflared_dns_names:
        raise AssertionError("cloudflared can resolve the webhook proxy")


def assert_scrubbing_proxy_boundary(machine: dict) -> None:
    listener_documents, _ = render("webhook-listener", [DEFAULTS, machine])
    listener = document(listener_documents, "Deployment", "webhook-listener")
    listener_env = {
        item["name"]: item for item in listener["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    expected_proxy = "http://webhook-egress-proxy.egress-proxy.svc.cluster.local:8080"
    if listener_env.get("WEBHOOK_FORWARD_PROXY", {}).get("value") != expected_proxy:
        raise AssertionError("listener is not pinned to the webhook scrubbing proxy")

    listener_policy = document(listener_documents, "CiliumNetworkPolicy", "webhook-listener")
    listener_egress = listener_policy["spec"]["egress"]
    if any(
        endpoint.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "agent-sandbox"
        for rule in listener_egress
        for endpoint in rule.get("toEndpoints", [])
    ):
        raise AssertionError("listener retains a direct agent egress path")

    proxy_documents, _ = render("egress-proxy", [DEFAULTS, machine])
    proxy = document(proxy_documents, "Deployment", "webhook-egress-proxy")
    proxy_pod = proxy["spec"]["template"]["spec"]
    if proxy_pod.get("automountServiceAccountToken") is not False:
        raise AssertionError("webhook scrubbing proxy mounts a service-account token")
    proxy_env = {
        item["name"]: item.get("value")
        for item in proxy_pod["containers"][0].get("env", [])
    }
    if proxy_env.get("WEBHOOK_PROXY_MODE") != "enabled":
        raise AssertionError("webhook scrubbing proxy did not enable its inbound gate")
    if proxy_env.get("PROMPT_INJECTION_DETECTION") != "disabled":
        raise AssertionError("webhook scrubbing proxy ignored the default prompt-injection status")

    service = document(proxy_documents, "Service", "webhook-egress-proxy")
    if service["spec"]["ports"] != [{"name": "proxy", "port": 8080, "targetPort": "proxy"}]:
        raise AssertionError("webhook scrubbing proxy Service exposes unexpected ports")

    policy = document(proxy_documents, "CiliumNetworkPolicy", "webhook-egress-proxy")
    ingress_namespaces = {
        endpoint["matchLabels"].get("io.kubernetes.pod.namespace")
        for rule in policy["spec"].get("ingress", [])
        for endpoint in rule.get("fromEndpoints", [])
    }
    if ingress_namespaces != {"webhooks"}:
        raise AssertionError(f"webhook scrubbing proxy accepts untrusted clients: {ingress_namespaces}")
    if any(
        endpoint.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "agentgateway-system"
        for rule in policy["spec"].get("egress", [])
        for endpoint in rule.get("toEndpoints", [])
    ):
        raise AssertionError("disabled prompt detection retained Agentgateway egress")

    ordinary_policy = document(proxy_documents, "CiliumNetworkPolicy", "egress-proxy")
    if any(
        endpoint.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "agent-sandbox"
        for rule in ordinary_policy["spec"].get("egress", [])
        for endpoint in rule.get("toEndpoints", [])
    ):
        raise AssertionError("ordinary agent proxy can reach agent webhook ports")

    enabled = copy.deepcopy(machine)
    enabled.setdefault("policy", {}).setdefault("contentSafety", {}).setdefault(
        "promptInjection", {}
    )["status"] = "enabled"
    enabled_documents, _ = render("egress-proxy", [DEFAULTS, enabled])
    enabled_proxy = document(enabled_documents, "Deployment", "webhook-egress-proxy")
    enabled_env = {
        item["name"]: item.get("value")
        for item in enabled_proxy["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    if enabled_env.get("PROMPT_INJECTION_DETECTION") != "enabled":
        raise AssertionError("enabled prompt-injection status did not reach the webhook proxy")
    enabled_policy = document(enabled_documents, "CiliumNetworkPolicy", "webhook-egress-proxy")
    if not any(
        endpoint.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "agentgateway-system"
        for rule in enabled_policy["spec"].get("egress", [])
        for endpoint in rule.get("toEndpoints", [])
    ):
        raise AssertionError("enabled prompt detection cannot reach its Agentgateway judge")


def assert_webhook_logs_collected() -> None:
    # Vector scopes by container, so every webhook workload needs its own clause.
    victoria_logs = yaml.safe_load((ROOT / "stages/values/victoria-logs.yaml").read_text(encoding="utf-8"))
    scope = victoria_logs["vector"]["customConfig"]["transforms"]["scope"]["condition"]["source"]
    for workload in ("webhook-listener", "cloudflared"):
        required = f'(ns == "webhooks" && pod_app_label == "{workload}" && container == "{workload}")'
        if required not in scope:
            raise AssertionError(f"Vector does not collect {workload} logs")


def assert_personal_alertmanager_prompts_resolve() -> None:
    personal = yaml.safe_load((ROOT / "examples/personal.yaml").read_text(encoding="utf-8"))
    routes = personal["agents"][0]["webhooks"]["routes"]
    payload = {
        "status": "firing",
        "commonLabels": {
            "alertname": "VicegerentWebhookTest",
            "severity": "warning",
            "namespace": "default",
        },
        "commonAnnotations": {
            "summary": "Synthetic webhook test",
            "description": "No production incident",
        },
    }
    for route_name in ("alertmanager-alerts", "alertmanager-alerts-test"):
        prompt = routes[route_name]["prompt"]
        unresolved = []
        for key in re.findall(r"\{([a-zA-Z0-9_.]+)\}", prompt):
            value = payload
            for part in key.split("."):
                if not isinstance(value, dict) or part not in value:
                    unresolved.append(key)
                    break
                value = value[part]
        if unresolved:
            raise AssertionError(
                f"{route_name} prompt placeholders do not match an Alertmanager payload: {unresolved}"
            )


def assert_incident_route_contracts() -> None:
    profiles = {
        "personal": (
            "alertmanager-alerts",
            "alertmanager-alerts-test",
            "alertmanager",
        ),
    }
    for profile, (production_name, test_name, provider) in profiles.items():
        values = yaml.safe_load((ROOT / "examples" / f"{profile}.yaml").read_text(encoding="utf-8"))
        webhooks = values["agents"][0]["webhooks"]
        routes = webhooks["routes"]
        if "unrestricted" in webhooks:
            raise AssertionError(f"{profile} retains the obsolete unrestricted boolean")
        if "test" in routes:
            raise AssertionError(f"{profile} retains the obsolete plain test route")
        if routes[production_name].get("provider") != provider:
            raise AssertionError(f"{production_name} lost its native provider")
        if routes[test_name].get("provider") != "generic-v2":
            raise AssertionError(f"{test_name} is not curl-testable through generic-v2")
        if routes[production_name]["prompt"] != routes[test_name]["prompt"]:
            raise AssertionError(f"{test_name} does not exercise the production investigation prompt")
        for route_name in (production_name, test_name):
            route = routes[route_name]
            if {"deliver", "respond"} & route.keys():
                raise AssertionError(f"{route_name} bypasses the asynchronous hermes send workflow")
            prompt = route["prompt"]
            required = (
                "hermes send --to slack --json",
                "open a draft pull or",
                "Do not merely recommend",
                "Never merge",
            )
            missing = [text for text in required if text not in prompt]
            if missing:
                raise AssertionError(f"{route_name} is missing incident workflow instructions: {missing}")


def main() -> int:
    assert_disabled()
    assert_invalid_values()
    machine = machine_values()
    assert_agent_boundary(machine)
    assert_webhook_toolset_override(machine)
    assert_multi_agent_routing()
    assert_listener(machine)
    assert_scrubbing_proxy_boundary(machine)
    assert_webhook_logs_collected()
    assert_personal_alertmanager_prompts_resolve()
    assert_incident_route_contracts()
    print("Webhook ingress render validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
