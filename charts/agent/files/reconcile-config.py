#!/usr/bin/env python3
"""Reconcile writable harness configs along explicit project-owned boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib
import yaml

MISSING = object()


def deep_merge(base: Any, override: Any) -> Any:
    """Return base recursively overlaid by override; lists and scalars replace."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return deepcopy(override)
    merged = deepcopy(base)
    for key, value in override.items():
        if key in merged:
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def get_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return MISSING
        current = current[key]
    return current


def set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = data
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = deepcopy(value)


def delete_path(data: dict[str, Any], path: tuple[str, ...]) -> None:
    current: Any = data
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return
        current = current[key]
    if isinstance(current, dict):
        current.pop(path[-1], None)


def replace_path(
    result: dict[str, Any], desired: dict[str, Any], path: tuple[str, ...]
) -> None:
    value = get_path(desired, path)
    if value is MISSING:
        delete_path(result, path)
    else:
        set_path(result, path, value)


def preserve_mapping_leaves(
    desired: dict[str, Any], existing: dict[str, Any], leaves: tuple[str, ...]
) -> dict[str, Any]:
    result = deepcopy(desired)
    for leaf in leaves:
        if leaf in existing:
            result[leaf] = deepcopy(existing[leaf])
    return result


def reconcile_route(
    result: dict[str, Any],
    desired: dict[str, Any],
    providers: dict[str, Any],
    default_provider: str,
) -> None:
    provider_name = result.get("provider", default_provider)
    if provider_name not in providers:
        result["provider"] = desired.get("provider", default_provider)
        if "default" in desired:
            result["default"] = deepcopy(desired["default"])
        if "model" in desired:
            result["model"] = deepcopy(desired["model"])
        provider_name = result["provider"]

    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        return
    result["provider"] = provider_name
    result["base_url"] = provider.get("api")
    result["key_env"] = provider.get("key_env")
    result["api_mode"] = provider.get("transport")


def reconcile_hermes(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    result = deep_merge(desired, existing)
    for path in (
        ("providers",),
        ("model_catalog",),
        ("model_aliases",),
        ("moa",),
        ("fallback_providers",),
        ("hooks",),
        ("platform_toolsets",),
        ("kanban",),
        ("mcp_servers",),
        ("toolsets",),
        ("agent", "disabled_toolsets"),
        ("command_allowlist",),
        ("approvals", "mode"),
        ("hooks_auto_accept",),
        ("custom_providers",),
        ("plugins",),
        ("slack",),
    ):
        replace_path(result, desired, path)

    providers = result.get("providers")
    desired_model = desired.get("model")
    existing_model = existing.get("model")
    if isinstance(desired_model, dict) and isinstance(existing_model, dict):
        result["model"] = preserve_mapping_leaves(
            desired_model,
            existing_model,
            ("default", "provider", "context_length", "persist_switch_by_default"),
        )
    desired_delegation = desired.get("delegation")
    existing_delegation = existing.get("delegation")
    if isinstance(desired_delegation, dict) and isinstance(existing_delegation, dict):
        result["delegation"] = preserve_mapping_leaves(
            desired_delegation,
            existing_delegation,
            ("model", "provider", "context_length"),
        )
    desired_auxiliary = desired.get("auxiliary")
    existing_auxiliary = existing.get("auxiliary")
    if isinstance(desired_auxiliary, dict):
        reconciled_auxiliary: dict[str, Any] = {}
        for name, desired_route in desired_auxiliary.items():
            if not isinstance(desired_route, dict):
                reconciled_auxiliary[name] = deepcopy(desired_route)
                continue
            existing_route = (
                existing_auxiliary.get(name, {})
                if isinstance(existing_auxiliary, dict)
                else {}
            )
            reconciled_auxiliary[name] = preserve_mapping_leaves(
                desired_route,
                existing_route if isinstance(existing_route, dict) else {},
                ("model", "provider", "context_length"),
            )
        result["auxiliary"] = reconciled_auxiliary

    model = result.get("model")
    if (
        isinstance(providers, dict)
        and isinstance(desired_model, dict)
        and isinstance(model, dict)
    ):
        reconcile_route(
            model,
            desired_model,
            providers,
            str(desired_model.get("provider", "anthropic")),
        )
        default_provider = str(
            model.get("provider", desired_model.get("provider", "anthropic"))
        )
        delegation = result.get("delegation")
        desired_delegation = desired.get("delegation")
        if isinstance(delegation, dict) and isinstance(desired_delegation, dict):
            reconcile_route(
                delegation,
                desired_delegation,
                providers,
                default_provider,
            )
        auxiliary = result.get("auxiliary")
        desired_auxiliary = desired.get("auxiliary")
        if isinstance(auxiliary, dict):
            for name, route in auxiliary.items():
                desired_route = (
                    desired_auxiliary.get(name, {})
                    if isinstance(desired_auxiliary, dict)
                    else {}
                )
                if isinstance(route, dict) and isinstance(desired_route, dict):
                    reconcile_route(route, desired_route, providers, default_provider)
    return result


def reconcile_claude_settings(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    result = deep_merge(desired, existing)
    for path in (
        ("env",),
        ("permissions",),
        ("sandbox",),
        ("enableAllProjectMcpServers",),
        ("skipDangerousModePermissionPrompt",),
    ):
        replace_path(result, desired, path)
    return result


def reconcile_claude_marketplaces(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    """Own the sandbox marketplace entries; leave user-added ones untouched."""
    result = deep_merge(desired, existing)
    for name in desired:
        replace_path(result, desired, (name,))
    return result


def reconcile_claude_plugins(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    """Seed plugin install records without reclaiming ones Claude Code owns."""
    return deep_merge(desired, existing)


def reconcile_claude_state(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    result = deep_merge(desired, existing)
    replace_path(result, desired, ("mcpServers",))
    desired_projects = desired.get("projects")
    if isinstance(desired_projects, dict):
        for project, settings in desired_projects.items():
            if isinstance(settings, dict) and "hasTrustDialogAccepted" in settings:
                set_path(
                    result,
                    ("projects", project, "hasTrustDialogAccepted"),
                    settings["hasTrustDialogAccepted"],
                )
    return result


def force_desired_mapping_leaves(
    result: dict[str, Any],
    desired: dict[str, Any],
    collection: str,
    leaf: str,
) -> None:
    desired_entries = desired.get(collection)
    if not isinstance(desired_entries, dict):
        return
    for name, settings in desired_entries.items():
        if isinstance(settings, dict) and leaf in settings:
            set_path(result, (collection, name, leaf), settings[leaf])


def reconcile_codex(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    result = deep_merge(desired, existing)
    for path in (
        ("model_provider",),
        ("model_providers",),
        ("mcp_servers",),
        ("web_search",),
        ("developer_instructions",),
        ("sandbox_mode",),
        ("approval_policy",),
        ("features", "respect_system_proxy"),
    ):
        replace_path(result, desired, path)
    force_desired_mapping_leaves(result, desired, "projects", "trust_level")
    return result


def reconcile_opencode(
    existing: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any]:
    result = deep_merge(desired, existing)
    for path in (("$schema",), ("provider",), ("mcp",), ("permission",), ("lsp",)):
        replace_path(result, desired, path)
    return result


def load_config(path: Path, fmt: str, *, empty_ok: bool = False) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if empty_ok and not text.strip():
        return {}
    if fmt == "yaml":
        data = yaml.safe_load(text)
    elif fmt == "json":
        data = json.loads(text)
    elif fmt == "toml":
        data = tomllib.loads(text)
    else:
        raise ValueError(f"unsupported format: {fmt}")
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a mapping")
    return data


def dump_config(data: dict[str, Any], fmt: str) -> str:
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if fmt == "toml":
        completed = subprocess.run(
            ["yq", "-p", "json", "-o", "toml"],
            input=json.dumps(data),
            text=True,
            check=True,
            capture_output=True,
        )
        return completed.stdout
    raise ValueError(f"unsupported format: {fmt}")


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(
            "usage: reconcile-config.py KIND FORMAT EXISTING DESIRED OUTPUT",
            file=sys.stderr,
        )
        return 2
    _, kind, fmt, existing_path, desired_path, output_path = argv
    existing = load_config(Path(existing_path), fmt, empty_ok=True)
    desired = load_config(Path(desired_path), fmt)
    if kind == "hermes":
        result = reconcile_hermes(existing, desired)
    elif kind == "claude-settings":
        result = reconcile_claude_settings(existing, desired)
    elif kind == "claude-state":
        result = reconcile_claude_state(existing, desired)
    elif kind == "claude-marketplaces":
        result = reconcile_claude_marketplaces(existing, desired)
    elif kind == "claude-plugins":
        result = reconcile_claude_plugins(existing, desired)
    elif kind == "codex":
        result = reconcile_codex(existing, desired)
    elif kind == "opencode":
        result = reconcile_opencode(existing, desired)
    else:
        raise ValueError(f"unsupported harness config kind: {kind}")
    write_atomic(Path(output_path), dump_config(result, fmt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
