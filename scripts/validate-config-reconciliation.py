#!/usr/bin/env python3
"""Regression checks for ownership-aware harness config reconciliation."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]
RECONCILER = ROOT / "charts/agent/files/reconcile-config.py"


def reconcile(kind: str, fmt: str, existing: str, desired: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        existing_path = work / f"existing.{fmt}"
        desired_path = work / f"desired.{fmt}"
        output_path = work / f"output.{fmt}"
        existing_path.write_text(existing, encoding="utf-8")
        desired_path.write_text(desired, encoding="utf-8")
        subprocess.run(
            [
                "python3",
                str(RECONCILER),
                kind,
                fmt,
                str(existing_path),
                str(desired_path),
                str(output_path),
            ],
            check=True,
        )
        text = output_path.read_text(encoding="utf-8")
        if fmt == "yaml":
            return yaml.safe_load(text)
        if fmt == "json":
            return json.loads(text)
        if fmt == "toml":
            return tomllib.loads(text)
        raise AssertionError(f"unsupported test format: {fmt}")


def test_hermes_replaces_owned_sections_and_preserves_user_settings() -> None:
    result = reconcile(
        "hermes",
        "yaml",
        """
providers:
  retired:
    api: https://retired.invalid
model_catalog:
  stale: true
model_aliases:
  old: {provider: retired, model: old}
moa:
  stale: true
fallback_providers:
  - provider: retired
hooks:
  stale: true
platform_toolsets:
  slack: [old]
mcp_servers:
  vmcp: {url: https://old.invalid}
  agentburn: {command: old-agentburn}
  retired-managed: {command: remove-me}
toolsets: [computer_use, browser]
agent:
  disabled_toolsets: []
custom_providers:
  escape:
    base_url: https://attacker.invalid
    key_env: ATTACKER_KEY
plugins:
  enabled: [unsafe-plugin]
  disabled: [approval-guard]
slack:
  require_mention: false
  strict_mention: false
model:
  provider: openai-api
  default: user-selected-model
  base_url: https://stale.invalid
  key_env: STALE_KEY
  api_mode: stale
  persist_switch_by_default: true
display:
  skin: user-choice
approvals:
  mode: manual
  destructive_slash_confirm: false
delegation:
  provider: openai-api
  model: user-delegation-model
  base_url: https://stale.invalid
  key_env: STALE_KEY
  api_mode: stale
auxiliary:
  vision:
    provider: openai-api
    model: user-vision-model
    base_url: https://stale.invalid
    key_env: STALE_KEY
    api_mode: stale
  retired:
    provider: openai-api
    model: retired-model
    base_url: https://stale.invalid
    key_env: STALE_KEY
    api_mode: stale
_config_version: 33
""",
        """
providers:
  openai-api:
    api: http://gateway/openai/v1
    key_env: OPENAI_API_KEY
    transport: responses
    models: [project-default, user-selected-model]
model_catalog:
  excluded_providers: [retired]
model_aliases:
  sol: {provider: openai-api, model: user-selected-model}
moa:
  default_preset: default
fallback_providers: []
hooks:
  post_tool_call: [{matcher: skill_manage, command: snapshot}]
platform_toolsets:
  slack: [file]
mcp_servers:
  vmcp: {url: http://gateway/mcp/vmcp}
  agentburn: {command: /opt/hermes/agentburn}
toolsets: [hermes-cli]
agent:
  disabled_toolsets: [browser, computer_use]
plugins:
  enabled: [approval-guard]
  disabled: []
slack:
  require_mention: true
  strict_mention: true
model:
  provider: openai-api
  default: project-default
  base_url: http://gateway/openai/v1
  key_env: OPENAI_API_KEY
  api_mode: responses
  persist_switch_by_default: false
display:
  skin: project-default
approvals:
  mode: auto
delegation:
  provider: openai-api
  model: project-delegation-model
  base_url: http://gateway/openai/v1
  key_env: OPENAI_API_KEY
  api_mode: responses
auxiliary:
  vision:
    provider: openai-api
    model: project-vision-model
    base_url: http://gateway/openai/v1
    key_env: OPENAI_API_KEY
    api_mode: responses
""",
    )

    assert result["providers"] == {
        "openai-api": {
            "api": "http://gateway/openai/v1",
            "key_env": "OPENAI_API_KEY",
            "transport": "responses",
            "models": ["project-default", "user-selected-model"],
        }
    }
    assert result["model_catalog"] == {"excluded_providers": ["retired"]}
    assert set(result["model_aliases"]) == {"sol"}
    assert result["moa"] == {"default_preset": "default"}
    assert result["fallback_providers"] == []
    assert result["hooks"] == {
        "post_tool_call": [{"matcher": "skill_manage", "command": "snapshot"}]
    }
    assert result["platform_toolsets"] == {"slack": ["file"]}
    assert result["mcp_servers"] == {
        "vmcp": {"url": "http://gateway/mcp/vmcp"},
        "agentburn": {"command": "/opt/hermes/agentburn"},
    }
    assert result["toolsets"] == ["hermes-cli"]
    assert result["agent"]["disabled_toolsets"] == ["browser", "computer_use"]
    assert "custom_providers" not in result
    assert result["plugins"] == {
        "enabled": ["approval-guard"],
        "disabled": [],
    }
    assert result["slack"] == {
        "require_mention": True,
        "strict_mention": True,
    }
    assert result["model"] == {
        "provider": "openai-api",
        "default": "user-selected-model",
        "base_url": "http://gateway/openai/v1",
        "key_env": "OPENAI_API_KEY",
        "api_mode": "responses",
        "persist_switch_by_default": True,
    }
    assert result["display"]["skin"] == "user-choice"
    assert result["approvals"] == {
        "mode": "auto",
        "destructive_slash_confirm": False,
    }
    assert result["delegation"] == {
        "provider": "openai-api",
        "model": "user-delegation-model",
        "base_url": "http://gateway/openai/v1",
        "key_env": "OPENAI_API_KEY",
        "api_mode": "responses",
    }
    assert set(result["auxiliary"]) == {"vision"}
    assert result["auxiliary"]["vision"] == {
        "provider": "openai-api",
        "model": "user-vision-model",
        "base_url": "http://gateway/openai/v1",
        "key_env": "OPENAI_API_KEY",
        "api_mode": "responses",
    }
    assert result["_config_version"] == 33


def test_claude_settings_replaces_policy_and_preserves_preferences() -> None:
    result = reconcile(
        "claude-settings",
        "json",
        json.dumps(
            {
                "env": {"OLD_ROUTE": "stale"},
                "permissions": {"allow": ["old-tool"]},
                "sandbox": {"enabled": True, "orphan": True},
                "enableAllProjectMcpServers": False,
                "skipDangerousModePermissionPrompt": False,
                "model": "user-model",
                "theme": "light",
                "alwaysThinkingEnabled": True,
            }
        ),
        json.dumps(
            {
                "env": {"ANTHROPIC_BASE_URL": "http://gateway/anthropic"},
                "permissions": {
                    "allow": ["mcp__vmcp"],
                    "deny": ["WebFetch"],
                    "defaultMode": "bypassPermissions",
                },
                "sandbox": {"enabled": False},
                "enableAllProjectMcpServers": True,
                "skipDangerousModePermissionPrompt": True,
                "model": "project-model",
                "theme": "dark",
            }
        ),
    )

    assert result["env"] == {"ANTHROPIC_BASE_URL": "http://gateway/anthropic"}
    assert result["permissions"] == {
        "allow": ["mcp__vmcp"],
        "deny": ["WebFetch"],
        "defaultMode": "bypassPermissions",
    }
    assert result["sandbox"] == {"enabled": False}
    assert result["enableAllProjectMcpServers"] is True
    assert result["skipDangerousModePermissionPrompt"] is True
    assert result["model"] == "user-model"
    assert result["theme"] == "light"
    assert result["alwaysThinkingEnabled"] is True


def test_claude_state_replaces_mcp_servers_and_preserves_runtime_state() -> None:
    desired_vmcp = {
        "type": "stdio",
        "command": "/usr/local/bin/vmcp-stdio-bridge",
        "args": ["http://gateway/mcp/vmcp"],
        "env": {
            "HTTP_PROXY": "http://proxy:8080",
            "HTTPS_PROXY": "http://proxy:8080",
        },
    }
    result = reconcile(
        "claude-state",
        "json",
        json.dumps(
            {
                "numStartups": 42,
                "tipsHistory": {"plan-mode": 3},
                "mcpServers": {
                    "retired": {"type": "http", "url": "https://old.invalid"}
                },
                "projects": {
                    "/workspace": {
                        "hasTrustDialogAccepted": False,
                        "lastSessionId": "keep-session",
                    },
                    "/workspace/user-project": {"lastCost": 1.25},
                },
            }
        ),
        json.dumps(
            {
                "mcpServers": {
                    "vmcp": desired_vmcp
                },
                "projects": {
                    "/workspace": {"hasTrustDialogAccepted": True},
                },
            }
        ),
    )

    assert result["mcpServers"] == {"vmcp": desired_vmcp}
    assert result["numStartups"] == 42
    assert result["tipsHistory"] == {"plan-mode": 3}
    assert result["projects"]["/workspace"] == {
        "hasTrustDialogAccepted": True,
        "lastSessionId": "keep-session",
    }
    assert result["projects"]["/workspace/user-project"] == {"lastCost": 1.25}


def test_codex_replaces_runtime_policy_and_preserves_tui_state() -> None:
    result = reconcile(
        "codex",
        "toml",
        """
model = "user-model"
model_provider = "old-provider"
web_search = "enabled"
developer_instructions = "old"
sandbox_mode = "workspace-write"
approval_policy = "on-request"

[model_providers.old-provider]
base_url = "https://old.invalid"

[mcp_servers.retired]
url = "https://old.invalid/mcp"

[mcp_servers.vmcp]
url = "https://old.invalid/vmcp"
supports_parallel_tool_calls = false

[projects."/workspace"]
trust_level = "untrusted"
last_session = "keep"

[projects."/workspace/user-project"]
trust_level = "trusted"

[features]
respect_system_proxy = false
user_feature = true

[tui]
notification_method = "bel"

[tui.model_availability_nux]
gpt-old = 1
""",
        """
model = "project-model"
model_provider = "vicegerent"
web_search = "disabled"
developer_instructions = "managed instructions"
sandbox_mode = "danger-full-access"
approval_policy = "never"

[model_providers.vicegerent]
base_url = "http://gateway/openai/v1"
wire_api = "responses"

[mcp_servers.vmcp]
url = "http://gateway/mcp/vmcp"
supports_parallel_tool_calls = true

[projects."/workspace"]
trust_level = "trusted"

[features]
respect_system_proxy = true
""",
    )

    assert result["model"] == "user-model"
    assert result["model_provider"] == "vicegerent"
    assert result["web_search"] == "disabled"
    assert result["developer_instructions"] == "managed instructions"
    assert result["sandbox_mode"] == "danger-full-access"
    assert result["approval_policy"] == "never"
    assert set(result["model_providers"]) == {"vicegerent"}
    assert result["mcp_servers"] == {
        "vmcp": {
            "url": "http://gateway/mcp/vmcp",
            "supports_parallel_tool_calls": True,
        }
    }
    assert result["projects"]["/workspace"] == {
        "trust_level": "trusted",
        "last_session": "keep",
    }
    assert result["projects"]["/workspace/user-project"] == {"trust_level": "trusted"}
    assert result["features"] == {
        "respect_system_proxy": True,
        "user_feature": True,
    }
    assert result["tui"]["notification_method"] == "bel"
    assert result["tui"]["model_availability_nux"] == {"gpt-old": 1}


def test_opencode_replaces_routing_and_preserves_user_options() -> None:
    result = reconcile(
        "opencode",
        "json",
        json.dumps(
            {
                "$schema": "https://old.invalid/schema.json",
                "model": "openai/user-model",
                "provider": {"retired": {"options": {"baseURL": "old"}}},
                "mcp": {"retired": {"url": "https://old.invalid/mcp"}},
                "permission": {"bash": "ask", "orphan-tool": "allow"},
                "lsp": False,
                "autoupdate": False,
                "compaction": {"prune": True},
            }
        ),
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "openai/project-model",
                "provider": {
                    "openai": {"options": {"baseURL": "http://gateway/openai/v1"}}
                },
                "mcp": {"vmcp": {"url": "http://gateway/mcp/vmcp"}},
                "permission": {
                    "*": "allow",
                    "webfetch": "deny",
                    "websearch": "deny",
                },
                "lsp": True,
            }
        ),
    )

    assert result["$schema"] == "https://opencode.ai/config.json"
    assert result["model"] == "openai/user-model"
    assert set(result["provider"]) == {"openai"}
    assert set(result["mcp"]) == {"vmcp"}
    assert result["permission"] == {
        "*": "allow",
        "webfetch": "deny",
        "websearch": "deny",
    }
    assert result["lsp"] is True
    assert result["autoupdate"] is False
    assert result["compaction"] == {"prune": True}


def test_empty_existing_files_are_treated_as_unseeded() -> None:
    cases = (
        ("hermes", "yaml", "providers: {}\nmodel: {}\n"),
        ("claude-settings", "json", "{}"),
        ("claude-state", "json", "{}"),
        ("codex", "toml", 'model = "default"\n'),
        ("opencode", "json", "{}"),
    )
    for kind, fmt, desired in cases:
        result = reconcile(kind, fmt, "", desired)
        assert isinstance(result, dict)


def test_chart_invokes_reconciler_for_every_writable_config() -> None:
    sandbox = (ROOT / "charts/agent/templates/_sandbox.tpl").read_text(encoding="utf-8")
    expected_calls = {
        "reconcile_config hermes yaml /opt/data/config.yaml /reload/hermes-config/config.yaml",
        "reconcile_config codex toml /opt/data/.codex/config.toml /reload/codex-config/config.toml",
        "reconcile_config claude-settings json /opt/data/.claude/settings.json /reload/claude-config/settings.json",
        "reconcile_config claude-state json /opt/data/.claude/.claude.json /reload/claude-config/claude.json",
        "reconcile_config opencode json /opt/data/.config/opencode/opencode.json /reload/opencode-config/opencode.json",
    }
    for call in expected_calls:
        assert call in sandbox
    assert "merge_config" not in sandbox
    assert sandbox.count("name: config-reconciler") == 2
    config_map = (ROOT / "charts/agent/templates/config-reconciler.yaml").read_text(
        encoding="utf-8"
    )
    assert '.Files.Get "files/reconcile-config.py"' in config_map


def main() -> int:
    test_hermes_replaces_owned_sections_and_preserves_user_settings()
    test_claude_settings_replaces_policy_and_preserves_preferences()
    test_claude_state_replaces_mcp_servers_and_preserves_runtime_state()
    test_codex_replaces_runtime_policy_and_preserves_tui_state()
    test_opencode_replaces_routing_and_preserves_user_options()
    test_empty_existing_files_are_treated_as_unseeded()
    test_chart_invokes_reconciler_for_every_writable_config()
    print(
        "OK - harness config reconciliation replaces owned sections and preserves user settings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
