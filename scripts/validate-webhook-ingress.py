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
        "webhooks": {"publicUrl": "https://hooks.example.ngrok.app"},
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
        "http://hooks.example.ngrok.app",
        "https://hooks.example.ngrok.app/",
        "https://hooks.example.ngrok.app/path",
        "https://hooks.example.ngrok.app?query=yes",
        "https://user@hooks.example.ngrok.app",
        "https://hooks.example.ngrok.app:443",
    ):
        machine = machine_values()
        machine["webhooks"]["publicUrl"] = invalid_url
        _, output = render("webhook-listener", [DEFAULTS, machine], expect_failure=True)
        if "webhooks.publicUrl" not in output:
            raise AssertionError(f"invalid public URL produced an unclear error: {invalid_url}")

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
    if len(documents) != 3:
        raise AssertionError(f"listener rendered {len(documents)} resources instead of 3")

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

    deployment = document(documents, "Deployment", "webhook-listener")
    if deployment["spec"].get("replicas") != 1 or deployment["spec"].get("strategy") != {"type": "Recreate"}:
        raise AssertionError("listener updates could race two replicas for one ngrok endpoint")
    pod = deployment["spec"]["template"]["spec"]
    if pod.get("automountServiceAccountToken") is not False:
        raise AssertionError("listener mounts a service-account token")
    if pod.get("dnsConfig", {}).get("options") != [{"name": "ndots", "value": "1"}]:
        raise AssertionError("listener DNS is not pinned for exact FQDN policy matches")
    container = pod["containers"][0]
    if container.get("resources", {}).get("requests") != {"cpu": "25m", "memory": "32Mi"}:
        raise AssertionError("listener has no scheduler resource floor")
    environment = {entry["name"]: entry for entry in container["env"]}
    token_ref = environment["NGROK_AUTHTOKEN"]["valueFrom"]["secretKeyRef"]
    if token_ref != {"name": "vicegerent-ngrok-authtoken", "key": "authtoken"}:
        raise AssertionError("listener ngrok credential has the wrong Secret reference")
    if environment["WEBHOOK_PUBLIC_URL"].get("value") != machine["webhooks"]["publicUrl"]:
        raise AssertionError("listener public URL drifted")

    secret_volume = next(volume for volume in pod["volumes"] if volume["name"] == "route-secrets")
    if secret_volume.get("secret") != {"defaultMode": 256, "secretName": "vicegerent-webhook-secrets"}:  # pragma: allowlist secret
        raise AssertionError("listener does not mount the shared webhook Secret")

    policy = document(documents, "CiliumNetworkPolicy", "webhook-listener")
    if "ingress" in policy["spec"] or len(policy["spec"].get("egress", [])) != 3:
        raise AssertionError("listener policy has an unexpected network direction")
    dns, ngrok, proxy = policy["spec"]["egress"]
    dns_names = {
        entry["matchName"]
        for entry in dns["toPorts"][0]["rules"]["dns"]
    }
    if dns["toEndpoints"] != [{"matchLabels": {"io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}]:
        raise AssertionError("listener DNS egress is not limited to kube-dns")
    if dns_names != {"connect.ngrok-agent.com", "webhook-egress-proxy.egress-proxy.svc.cluster.local"}:
        raise AssertionError(f"listener DNS names are too broad: {dns_names}")
    if ngrok != {
        "toFQDNs": [{"matchName": "connect.ngrok-agent.com"}],
        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
    }:
        raise AssertionError(f"listener ngrok egress is too broad: {ngrok}")
    if proxy != {
        "toEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "egress-proxy", "app.kubernetes.io/name": "webhook-egress-proxy"}}],
        "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
    }:
        raise AssertionError(f"listener proxy egress is too broad: {proxy}")


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


def assert_listener_logs_collected() -> None:
    victoria_logs = yaml.safe_load((ROOT / "stages/values/victoria-logs.yaml").read_text(encoding="utf-8"))
    scope = victoria_logs["vector"]["customConfig"]["transforms"]["scope"]["condition"]["source"]
    required = '(ns == "webhooks" && pod_app_label == "webhook-listener" && container == "webhook-listener")'
    if required not in scope:
        raise AssertionError("Vector does not collect webhook-listener delivery logs")


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
        "work": (
            "pagerduty-incidents",
            "pagerduty-incidents-test",
            "pagerduty",
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
    assert_listener_logs_collected()
    assert_personal_alertmanager_prompts_resolve()
    assert_incident_route_contracts()
    print("Webhook ingress render validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
