#!/usr/bin/env python3
"""Validate the merged machine-values API before installation or rendering."""

from __future__ import annotations

import argparse
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

PROVIDERS = ("anthropic", "openai", "deepseek", "zai")
RELEASE_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
WEBHOOK_ROUTE_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}$")
WEBHOOK_PROVIDERS = {"github", "gitlab", "pagerduty", "svix", "generic-v2", "alertmanager"}
WEBHOOK_ROUTE_FIELDS = {
    "provider",
    "enabled",
    "description",
    "events",
    "prompt",
    "skills",
    "filters",
    "script",
    "deliver",
    "deliver_extra",
    "deliver_only",
}
ANY_MAPPING_PATHS = {
    ("agentDefaults", "config"),
    ("agents", "config"),
    ("agentDefaults", "webhooks", "routes"),
    ("agents", "webhooks", "routes"),
    ("agentDefaults", "directEgress", "ssh", "hosts"),
    ("agents", "directEgress", "ssh", "hosts"),
}
ANY_LIST_PATHS = {
    ("policy", "sourceControl", "github", "allowedRepos"),
    ("policy", "sourceControl", "github", "forkRequiredOwners"),
    ("policy", "sourceControl", "github", "forkOwners"),
    ("policy", "workManagement", "jira", "allowedProjects"),
    ("policy", "workManagement", "jira", "allowedAssignees"),
    ("policy", "workManagement", "jira", "allowedIssueTypes"),
    ("policy", "workManagement", "linear", "allowedTeams"),
    ("policy", "workManagement", "linear", "allowedAssignees"),
    ("policy", "workManagement", "pagerduty", "allowedServiceIds"),
    ("policy", "dataAccess", "grafana", "deniedDatasourceUids"),
    ("policy", "dataAccess", "grafana", "deniedDatasourceNames"),
    ("policy", "dataAccess", "elastic", "deniedIndexPatterns"),
    ("policy", "notion", "allowedParentPageIds"),
    ("egress", "wildcardDomains"),
    ("egress", "exactDomains"),
    ("egress", "internalAllowedCIDRs"),
    ("agentDefaults", "directEgress", "slackFQDNs"),
    ("agentDefaults", "directEgress", "edgeTtsFQDNs"),
    ("agents", "directEgress", "slackFQDNs"),
    ("agents", "directEgress", "edgeTtsFQDNs"),
}


def merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return deepcopy(override)
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if key in result else deepcopy(value)
    return result


def path_text(path: tuple[str, ...]) -> str:
    result = ""
    for part in path:
        result += part if part.startswith("[") else ("." if result else "") + part
    return result


def schema_path(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(re.sub(r"\[\d+\]$", "", part) for part in path)


def validate_value(value: Any, template: Any, path: tuple[str, ...], errors: list[str]) -> None:
    normalized = schema_path(path)
    if normalized in ANY_MAPPING_PATHS:
        if not isinstance(value, dict):
            errors.append(f"{path_text(path)}: expected mapping")
        return
    if normalized in ANY_LIST_PATHS:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{path_text(path)}: expected list of strings")
        return
    if isinstance(template, dict):
        if not isinstance(value, dict):
            errors.append(f"{path_text(path)}: expected mapping")
            return
        for key, child in value.items():
            if key not in template:
                errors.append(f"{path_text(path + (key,))}: unknown key")
                continue
            validate_value(child, template[key], path + (key,), errors)
        return
    if isinstance(template, list):
        if not isinstance(value, list):
            errors.append(f"{path_text(path)}: expected list")
        return
    if isinstance(template, bool):
        if not isinstance(value, bool):
            errors.append(f"{path_text(path)}: expected boolean")
        return
    if isinstance(template, int):
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path_text(path)}: expected integer")
        return
    if isinstance(template, str) and not isinstance(value, str):
        errors.append(f"{path_text(path)}: expected string")


def validate_ssh_hosts(value: dict[str, Any], path: tuple[str, ...], errors: list[str]) -> None:
    hosts = value.get("hosts")
    if hosts is None:
        return
    if not isinstance(hosts, dict):
        errors.append(f"{path_text(path + ('hosts',))}: expected mapping")
        return
    for host, config in hosts.items():
        host_path = path + ("hosts", str(host))
        if not isinstance(host, str) or not host:
            errors.append(f"{path_text(host_path)}: host name must be a non-empty string")
        if not isinstance(config, dict):
            errors.append(f"{path_text(host_path)}: expected mapping")
            continue
        unexpected = set(config) - {"cnameChain"}
        for key in sorted(unexpected):
            errors.append(f"{path_text(host_path + (key,))}: unknown key")
        cname_chain = config.get("cnameChain", [])
        if not isinstance(cname_chain, list) or not all(isinstance(item, str) for item in cname_chain):
            errors.append(f"{path_text(host_path + ('cnameChain',))}: expected list of strings")


def capacity_enabled(platform: dict[str, Any], provider: str) -> bool:
    return platform.get("models", {}).get(provider, {}).get("enabled") is True


def require_capacity(errors: list[str], platform: dict[str, Any], path: str, provider: str) -> None:
    if provider and not capacity_enabled(platform, provider):
        errors.append(f"{path} requires models.{provider}.enabled")


def validate_webhook_config(value: dict[str, Any], path: tuple[str, ...], errors: list[str]) -> None:
    toolsets = value.get("toolsets")
    if not isinstance(toolsets, list) or not all(isinstance(item, str) and item for item in toolsets):
        errors.append(f"{path_text(path + ('toolsets',))}: expected list of strings")
    elif len(toolsets) != len(set(toolsets)):
        errors.append(f"{path_text(path + ('toolsets',))}: duplicate entries are not allowed")

    routes = value.get("routes")
    if not isinstance(routes, dict):
        return
    for route_name, route in routes.items():
        route_path = path + ("routes", str(route_name))
        if not isinstance(route_name, str) or not WEBHOOK_ROUTE_NAME.fullmatch(route_name) or "--" in route_name:
            errors.append(
                f"{path_text(route_path)}: route name must match ^[a-z][a-z0-9-]{{0,62}}$ without consecutive hyphens"
            )
        if not isinstance(route, dict):
            errors.append(f"{path_text(route_path)}: expected mapping")
            continue
        for field in sorted(set(route) - WEBHOOK_ROUTE_FIELDS):
            errors.append(f"{path_text(route_path + (field,))}: unknown key")

        provider = route.get("provider")
        if provider not in WEBHOOK_PROVIDERS:
            errors.append(f"{path_text(route_path + ('provider',))}: unsupported provider")
        if "enabled" in route and not isinstance(route["enabled"], bool):
            errors.append(f"{path_text(route_path + ('enabled',))}: expected boolean")
        for field in ("description", "prompt", "script", "deliver"):
            if field in route and not isinstance(route[field], str):
                errors.append(f"{path_text(route_path + (field,))}: expected string")
        for field in ("events", "skills"):
            if field in route and (
                not isinstance(route[field], list)
                or not all(isinstance(item, str) and item for item in route[field])
            ):
                errors.append(f"{path_text(route_path + (field,))}: expected list of strings")
        if "filters" in route:
            filters = route["filters"]
            if not isinstance(filters, (dict, list)) or (
                isinstance(filters, list) and not all(isinstance(item, dict) for item in filters)
            ):
                errors.append(f"{path_text(route_path + ('filters',))}: expected mapping or list of mappings")
        if "deliver_extra" in route and not isinstance(route["deliver_extra"], dict):
            errors.append(f"{path_text(route_path + ('deliver_extra',))}: expected mapping")
        if "deliver_only" in route and not isinstance(route["deliver_only"], bool):
            errors.append(f"{path_text(route_path + ('deliver_only',))}: expected boolean")
        if route.get("deliver_only") is True and route.get("deliver", "log") == "log":
            errors.append(f"{path_text(route_path + ('deliver_only',))}: requires a non-log delivery target")


def validate_cross_field_contract(defaults: dict[str, Any], machine: dict[str, Any], errors: list[str]) -> None:
    platform = merge(defaults, machine)
    agent_defaults = merge(defaults.get("agentDefaults", {}), machine.get("agentDefaults", {}))
    statuses = platform.get("policy", {}).get("contentSafety", {})
    for feature in ("moderation", "promptInjection"):
        status_path = f"policy.contentSafety.{feature}.status"
        status = statuses.get(feature, {}).get("status")
        if status not in {"enabled", "disabled"}:
            errors.append(f"{status_path}: expected one of enabled, disabled")
        elif status == "enabled":
            require_capacity(errors, platform, status_path, "openai")

    names: dict[str, int] = {}
    for index, configured in enumerate(machine.get("agents", [])):
        path = f"agents[{index}]"
        if not isinstance(configured, dict):
            continue
        name = configured.get("name")
        if not isinstance(name, str) or not name or len(name) > 53 or not RELEASE_NAME.fullmatch(name):
            errors.append(f"{path}.name: must be a valid Kubernetes/Helm release name")
        elif name in names:
            errors.append(f"{path}.name: duplicates agents[{names[name]}].name")
        else:
            names[name] = index

        agent = merge(agent_defaults, configured)
        webhooks = agent.get("webhooks")
        if isinstance(webhooks, dict):
            # A webhook Secret key is "<agent>__<route>", so consecutive hyphens in
            # the agent name map to the "__" separator and collide across routes.
            if isinstance(name, str) and "--" in name:
                errors.append(f"{path}.name: must not contain consecutive hyphens for a webhook-enabled agent")
            validate_webhook_config(webhooks, (f"agents[{index}]", "webhooks"), errors)
        providers = agent.get("providers", {})
        for provider in PROVIDERS:
            if providers.get(provider, {}).get("enabled") is True:
                require_capacity(errors, platform, f"{path}.providers.{provider}.enabled", provider)
        if agent.get("harnesses", {}).get("claudeCode"):
            require_capacity(errors, platform, f"{path}.harnesses.claudeCode", "anthropic")
        for field in ("failover", "mnemosyne"):
            provider = agent.get(field, {}).get("provider")
            if isinstance(provider, str) and provider:
                require_capacity(errors, platform, f"{path}.{field}.provider", provider)
        moa_provider = agent.get("moa", {}).get("aggregator", {}).get("provider")
        if isinstance(moa_provider, str) and moa_provider:
            require_capacity(errors, platform, f"{path}.moa.aggregator.provider", moa_provider)
        direct_egress = configured.get("directEgress")
        if isinstance(direct_egress, dict) and isinstance(direct_egress.get("ssh"), dict):
            validate_ssh_hosts(direct_egress["ssh"], (f"agents[{index}]", "directEgress", "ssh"), errors)

    configured_defaults = machine.get("agentDefaults", {})
    if isinstance(configured_defaults, dict):
        direct_egress = configured_defaults.get("directEgress")
        if isinstance(direct_egress, dict) and isinstance(direct_egress.get("ssh"), dict):
            validate_ssh_hosts(direct_egress["ssh"], ("agentDefaults", "directEgress", "ssh"), errors)


def validate(defaults: dict[str, Any], machine: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(machine, dict):
        return ["values: expected mapping"]
    validate_value(machine, defaults, (), errors)
    agents = machine.get("agents")
    if not isinstance(agents, list):
        errors.append("agents: expected list")
    else:
        template = defaults.get("agentDefaults", {})
        for index, agent in enumerate(agents):
            if not isinstance(agent, dict):
                errors.append(f"agents[{index}]: expected mapping")
                continue
            agent_template = {**template, "name": ""}
            validate_value(agent, agent_template, (f"agents[{index}]",), errors)
    if errors:
        return errors
    validate_cross_field_contract(defaults, machine, errors)
    return errors


def load(path: Path) -> dict[str, Any]:
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--defaults", type=Path, required=True)
    parser.add_argument("values", nargs="+", type=Path)
    args = parser.parse_args()
    defaults = load(args.defaults)
    errors = [
        f"{values}: {error}"
        for values in args.values
        for error in validate(defaults, load(values))
    ]
    if errors:
        for error in errors:
            print(f"FAIL - {error}", file=sys.stderr)
        return 1
    print("PASS - merged machine values satisfy the configuration contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
