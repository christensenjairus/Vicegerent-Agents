#!/usr/bin/env python3
"""Require vMCP parallelism and explicit Agentburn serialization."""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PARALLEL_SERVER = "vmcp"
SERIAL_SERVER = "agentburn"
BATCH_GUIDANCE = "in batches of up to eight"
CLAUDE_BATCH_TOOL = "mcp__vmcp__batch_call_tool"
CLAUDE_BRIDGE = "/usr/local/bin/vmcp-stdio-bridge"
VMCP_URL = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp"
PROXY_URL = "http://egress-proxy.egress-proxy.svc.cluster.local:8080"


def main() -> None:
    defaults = yaml.safe_load((REPO / "values.defaults.yaml").read_text())
    example = yaml.safe_load((REPO / "values.example.yaml").read_text())

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        defaults_slice = tmpdir / "defaults.yaml"
        agent_slice = tmpdir / "agent.yaml"
        defaults_slice.write_text(yaml.safe_dump(defaults["agentDefaults"]))
        agent_slice.write_text(yaml.safe_dump(example["agents"][0]))
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "agent",
                "charts/agent",
                "-f",
                str(defaults_slice),
                "-f",
                str(agent_slice),
            ],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    config_maps = [
        document
        for document in yaml.safe_load_all(rendered)
        if document and document.get("kind") == "ConfigMap"
    ]

    def rendered_data(key: str) -> str:
        return next(
            document["data"][key]
            for document in config_maps
            if key in document.get("data", {})
        )

    config = yaml.safe_load(rendered_data("config.yaml"))
    servers = config.get("mcp_servers", {})
    servers = servers if isinstance(servers, dict) else {}
    vmcp = servers.get(PARALLEL_SERVER, {})
    vmcp = vmcp if isinstance(vmcp, dict) else {}
    elicitation = vmcp.get("elicitation", {})
    elicitation = elicitation if isinstance(elicitation, dict) else {}
    agentburn = servers.get(SERIAL_SERVER, {})
    agentburn = agentburn if isinstance(agentburn, dict) else {}
    codex = tomllib.loads(rendered_data("config.toml"))
    codex_servers = codex.get("mcp_servers", {})
    codex_vmcp = codex_servers.get(PARALLEL_SERVER, {})
    codex_mnemosyne = codex_servers.get("mnemosyne", {})
    claude = json.loads(rendered_data("claude.json"))
    claude_vmcp = claude.get("mcpServers", {}).get(PARALLEL_SERVER, {})
    harness_prompts = {
        "Hermes": rendered_data("SOUL.md"),
        "Claude Code": rendered_data("CLAUDE.md"),
        "Codex": codex.get("developer_instructions", ""),
        "OpenCode": rendered_data("AGENTS.md"),
    }
    failures = []
    if vmcp.get("supports_parallel_tool_calls") is not True:
        failures.append("vmcp must set supports_parallel_tool_calls: true")
    if elicitation.get("enabled") is not False:
        failures.append("vmcp must disable elicitation while calls share one connection")
    if agentburn.get("supports_parallel_tool_calls") is not False:
        failures.append(
            "agentburn must set supports_parallel_tool_calls: false explicitly"
        )
    if codex_vmcp.get("supports_parallel_tool_calls") is not True:
        failures.append("Codex vmcp must set supports_parallel_tool_calls: true")
    if codex_mnemosyne.get("supports_parallel_tool_calls") is True:
        failures.append("Codex mnemosyne must not opt into server-wide parallel calls")
    expected_claude_vmcp = {
        "type": "stdio",
        "command": CLAUDE_BRIDGE,
        "args": [VMCP_URL],
        "env": {
            "HTTP_PROXY": PROXY_URL,
            "HTTPS_PROXY": PROXY_URL,
            "http_proxy": PROXY_URL,
            "https_proxy": PROXY_URL,
        },
    }
    if claude_vmcp != expected_claude_vmcp:
        failures.append("Claude Code vmcp must use the managed stdio batch bridge")
    for harness, prompt in harness_prompts.items():
        if BATCH_GUIDANCE not in prompt:
            failures.append(f"{harness} must receive vMCP batching guidance")
    if CLAUDE_BATCH_TOOL not in harness_prompts["Claude Code"]:
        failures.append("Claude Code must receive batch_call_tool guidance")
    for harness in ("Hermes", "Codex", "OpenCode"):
        if CLAUDE_BATCH_TOOL in harness_prompts[harness]:
            failures.append(f"{harness} must not receive Claude-only batch tool guidance")
    bridge = REPO / "images/hermes/vmcp-bridge/vmcp-stdio-bridge.py"
    dockerfile = (REPO / "images/hermes/Dockerfile").read_text()
    if not bridge.is_file() or "vmcp-bridge/vmcp-stdio-bridge.py" not in dockerfile:
        failures.append("Claude Code vmcp bridge must be baked into the Hermes image")
    if failures:
        raise SystemExit("FAIL - " + "; ".join(failures))
    print(
        "PASS - vMCP parallel policy and harness-specific batching cover all four harnesses"
    )


if __name__ == "__main__":
    main()
