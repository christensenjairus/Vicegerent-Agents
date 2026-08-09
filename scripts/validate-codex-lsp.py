#!/usr/bin/env python3
"""Assert Codex uses the baked codex-lsp MCP runtime."""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
RUNTIME = "/opt/vicegerent/codex-lsp/cli.js"
REVISION = "e7c65b04d0cc549f0478d3b78b51714fc0f572b3"  # pragma: allowlist secret


def fail(message: str) -> None:
    raise SystemExit(f"FAIL - {message}")


def render_codex_config() -> dict:
    defaults = yaml.safe_load((REPO / "values.defaults.yaml").read_text(encoding="utf-8"))
    example = yaml.safe_load((REPO / "values.example.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        defaults_path = work / "defaults.yaml"
        agent_path = work / "agent.yaml"
        defaults_path.write_text(yaml.safe_dump(defaults["agentDefaults"]), encoding="utf-8")
        agent_path.write_text(yaml.safe_dump(example["agents"][0]), encoding="utf-8")
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "agent",
                str(REPO / "charts/agent"),
                "-f",
                str(defaults_path),
                "-f",
                str(agent_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    documents = [document for document in yaml.safe_load_all(rendered) if document]
    config = next(
        document["data"]["config.toml"]
        for document in documents
        if document.get("kind") == "ConfigMap" and "config.toml" in document.get("data", {})
    )
    return tomllib.loads(config)


def main() -> None:
    config = render_codex_config()
    lsp = config.get("mcp_servers", {}).get("lsp")
    expected = {
        "command": "node",
        "args": [RUNTIME, "mcp"],
        "tool_timeout_sec": 90,
    }
    if lsp != expected:
        fail(f"Codex lsp MCP must be {json.dumps(expected, sort_keys=True)}")

    dockerfile = (REPO / "images/agent/Dockerfile").read_text(encoding="utf-8")
    required = (
        f"CODEX_LSP_TOOLS_MCP_REVISION={REVISION}",
        "github.com/code-yeongyu/lsp-tools-mcp/archive/${CODEX_LSP_TOOLS_MCP_REVISION}.tar.gz",
        "tsc -p /tmp/codex-lsp/tsconfig.build.json",
        "node /opt/vicegerent/codex-lsp/cli.js mcp </dev/null",
    )
    missing = [value for value in required if value not in dockerfile]
    if missing:
        fail(f"Agent image must build and smoke-test codex-lsp: {', '.join(missing)}")

    packages = json.loads((REPO / "images/agent/package.json").read_text(encoding="utf-8"))
    if packages.get("dependencies", {}).get("@types/node") != "25.9.5":
        fail("Agent image must provide @types/node 25.9.5 to compile codex-lsp")

    print("OK - Codex launches the baked codex-lsp MCP runtime")


if __name__ == "__main__":
    main()
