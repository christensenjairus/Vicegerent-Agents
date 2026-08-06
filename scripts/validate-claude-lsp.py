#!/usr/bin/env python3
"""Assert Claude Code loads the image's language servers through the baked plugin."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = "/opt/vicegerent/claude-lsp"
PLUGIN_TREE = REPO / "images/hermes/claude-lsp"
MARKETPLACE = "vicegerent"
PLUGIN = "sandbox-lsp"
QUALIFIED = f"{PLUGIN}@{MARKETPLACE}"


def fail(message: str) -> None:
    raise SystemExit(f"FAIL - {message}")


def render_claude_config() -> dict[str, str]:
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
    return next(
        document["data"]
        for document in documents
        if document.get("kind") == "ConfigMap" and "settings.json" in document.get("data", {})
    )


def check_settings(data: dict[str, str]) -> None:
    settings = json.loads(data["settings.json"])
    marketplace = settings.get("extraKnownMarketplaces", {}).get(MARKETPLACE)
    expected = {"source": {"source": "directory", "path": PLUGIN_ROOT}}
    if marketplace != expected:
        fail(f"settings.json must register {MARKETPLACE} as {json.dumps(expected, sort_keys=True)}")
    if settings.get("enabledPlugins", {}).get(QUALIFIED) is not True:
        fail(f"settings.json must enable {QUALIFIED}")


def check_plugin_seed(data: dict[str, str]) -> None:
    """Claude Code registers marketplaces after its LSP manager starts, so a cold
    config directory needs both plugin state files seeded to load servers on the
    first session."""
    known = json.loads(data["plugins-known-marketplaces.json"])
    entry = known.get(MARKETPLACE, {})
    if entry.get("source", {}).get("path") != PLUGIN_ROOT:
        fail(f"plugins-known-marketplaces.json must source {MARKETPLACE} from {PLUGIN_ROOT}")
    if entry.get("installLocation") != PLUGIN_ROOT:
        fail(f"plugins-known-marketplaces.json must install {MARKETPLACE} at {PLUGIN_ROOT}")

    installed = json.loads(data["plugins-installed.json"])
    if installed.get("version") != 2:
        fail("plugins-installed.json must use install-record version 2")
    records = installed.get("plugins", {}).get(QUALIFIED, [])
    install_path = f"{PLUGIN_ROOT}/plugins/{PLUGIN}"
    if not any(record.get("installPath") == install_path for record in records):
        fail(f"plugins-installed.json must install {QUALIFIED} from {install_path}")


def check_sandbox_seeding() -> None:
    sandbox = (REPO / "charts/agent/templates/_sandbox.tpl").read_text(encoding="utf-8")
    required = (
        "reconcile_config claude-marketplaces json"
        " /opt/data/.claude/plugins/known_marketplaces.json",
        "reconcile_config claude-plugins json /opt/data/.claude/plugins/installed_plugins.json",
    )
    missing = [value for value in required if value not in sandbox]
    if missing:
        fail(f"sandbox startup must seed Claude Code plugin state: {', '.join(missing)}")


def check_plugin_tree() -> None:
    marketplace = json.loads(
        (PLUGIN_TREE / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    if marketplace.get("name") != MARKETPLACE:
        fail(f"baked marketplace must be named {MARKETPLACE}")
    sources = {plugin.get("name"): plugin.get("source") for plugin in marketplace.get("plugins", [])}
    if sources.get(PLUGIN) != f"./plugins/{PLUGIN}":
        fail(f"baked marketplace must expose {PLUGIN} from ./plugins/{PLUGIN}")

    plugin = json.loads(
        (PLUGIN_TREE / f"plugins/{PLUGIN}/.claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    if plugin.get("name") != PLUGIN:
        fail(f"baked plugin manifest must be named {PLUGIN}")

    servers = json.loads((PLUGIN_TREE / f"plugins/{PLUGIN}/.lsp.json").read_text(encoding="utf-8"))
    if not servers:
        fail("baked plugin must declare at least one language server")
    owners: dict[str, str] = {}
    for name, server in servers.items():
        command = server.get("command", "")
        if not command:
            fail(f"{name} must declare a command")
        if " " in command and not command.startswith("/"):
            fail(f"{name} command must be absolute when it contains a space")
        if command.startswith("/") and not command.startswith(f"{PLUGIN_ROOT}/"):
            fail(f"{name} absolute command must live under {PLUGIN_ROOT}")
        extensions = server.get("extensionToLanguage") or {}
        if not extensions:
            fail(f"{name} must map at least one file extension")
        # Claude Code binds each extension to the first server that claims it and
        # silently drops later claims, so overlap would disable a server.
        for extension in extensions:
            if extension in owners:
                fail(f"{extension} is claimed by both {owners[extension]} and {name}")
            owners[extension] = name


def check_dockerfile() -> None:
    dockerfile = (REPO / "images/hermes/Dockerfile").read_text(encoding="utf-8")
    required = (
        f"COPY claude-lsp/ {PLUGIN_ROOT}/",
        f"chmod +x {PLUGIN_ROOT}/bin/typescript-lsp",
        f"claude plugin validate {PLUGIN_ROOT}",
    )
    missing = [value for value in required if value not in dockerfile]
    if missing:
        fail(f"Hermes image must bake and validate the Claude LSP plugin: {', '.join(missing)}")


def main() -> None:
    data = render_claude_config()
    check_settings(data)
    check_plugin_seed(data)
    check_sandbox_seeding()
    check_plugin_tree()
    check_dockerfile()
    print("OK - Claude Code loads the image's language servers from the baked plugin")


if __name__ == "__main__":
    main()
