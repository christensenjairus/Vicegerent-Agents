#!/usr/bin/env python3
"""Ensure /model keeps GPT switches on openai-api through Agentgateway."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent


def run(*args: str) -> str:
    proc = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    if proc.returncode:
        raise SystemExit(f"FAIL - {' '.join(args)}\n{proc.stderr}")
    return proc.stdout


def extract_config(rendered: str) -> dict:
    match = re.search(r"^(\s*)config\.yaml:\s*\|-?\n(.*)", rendered, re.S | re.M)
    if not match:
        raise SystemExit("FAIL - rendered agent chart has no config.yaml")
    block = match.group(2)
    first = next(line for line in block.splitlines() if line.strip())
    indent = len(first) - len(first.lstrip())
    kept = []
    for line in block.splitlines():
        if line.strip() and not line.startswith(" " * indent):
            break
        kept.append(line[indent:] if len(line) >= indent else line)
    return yaml.safe_load("\n".join(kept)) or {}


def main() -> int:
    failures = []
    rendered_configs = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        defaults = tmpdir / "defaults.yaml"
        defaults.write_text(run("yq", ".agentDefaults", "values.defaults.yaml"))

        for example in ("values.example.yaml", "examples/personal.yaml", "examples/work.yaml"):
            agent = tmpdir / (Path(example).stem + ".yaml")
            agent.write_text(run("yq", ".agents[0]", example))
            rendered = run(
                "helm", "template", "agent", "charts/agent",
                "-f", str(defaults), "-f", str(agent),
            )
            config = extract_config(rendered)
            rendered_configs.append((example, rendered, config))
            providers = config.get("providers") or {}
            for alias, spec in (config.get("model_aliases") or {}).items():
                if not isinstance(spec, dict):
                    continue
                provider = spec.get("provider")
                model = spec.get("model")
                if provider not in {"openai", "openai-api"}:
                    continue
                if provider != "openai-api":
                    failures.append(
                        f"{example}: model_aliases.{alias} uses provider {provider!r}; "
                        "Hermes aliases 'openai' to OpenRouter, so it must use 'openai-api'"
                    )
                    continue
                declared = (providers.get(provider) or {}).get("models") or []
                if model not in declared:
                    failures.append(
                        f"{example}: model_aliases.{alias} routes {model!r} to "
                        f"{provider!r}, but providers.{provider}.models does not declare it"
                    )

        if not failures:
            try:
                from hermes_cli.model_switch import (
                    DIRECT_ALIASES,
                    list_authenticated_providers,
                    switch_model,
                )
            except ImportError:
                switch_model = None
            if switch_model is not None:
                old_home = os.environ.get("HERMES_HOME")
                old_base = os.environ.get("OPENAI_BASE_URL")
                old_key = os.environ.get("OPENAI_API_KEY")
                try:
                    for index, (example, rendered, config) in enumerate(rendered_configs):
                        aliases = config.get("model_aliases") or {}
                        gpt_models = sorted({
                            spec.get("model") for spec in aliases.values()
                            if isinstance(spec, dict)
                            and spec.get("provider") == "openai-api"
                            and spec.get("model")
                        })
                        if not gpt_models:
                            continue
                        match = re.search(
                            r"name:\s*OPENAI_BASE_URL\s*\n\s*value:\s*(\S+)", rendered
                        )
                        if not match:
                            failures.append(f"{example}: OPENAI_BASE_URL is not rendered")
                            continue
                        gateway_url = match.group(1).strip('"')
                        home = Path(tmp) / f"home-{index}"
                        home.mkdir()
                        (home / "config.yaml").write_text(yaml.safe_dump(config))
                        os.environ["HERMES_HOME"] = str(home)
                        os.environ["OPENAI_BASE_URL"] = gateway_url
                        os.environ["OPENAI_API_KEY"] = "test-value"  # pragma: allowlist secret
                        rows = list_authenticated_providers(
                            current_provider="anthropic",
                            current_base_url=config["providers"]["anthropic"]["api"],
                            current_model="claude-sonnet-5",
                            user_providers=config["providers"],
                            custom_providers=[],
                            max_models=10,
                            probe_custom_providers=False,
                            excluded_providers=(
                                (config.get("model_catalog") or {}).get("excluded_providers")
                                or []
                            ),
                        )
                        openai_rows = [row for row in rows if row["slug"] == "openai-api"]
                        if (
                            len(openai_rows) != 1
                            or not openai_rows[0]["is_user_defined"]
                            or openai_rows[0].get("api_url") != gateway_url
                        ):
                            failures.append(
                                f"{example}: /model picker did not expose exactly the "
                                "configured Agentgateway openai-api row"
                            )
                        DIRECT_ALIASES.clear()
                        for model in gpt_models:
                            result = switch_model(
                                model,
                                current_provider="anthropic",
                                current_model="claude-sonnet-5",
                                current_base_url=(config["providers"]["anthropic"]["api"]),
                                current_api_key="none",  # pragma: allowlist secret
                                user_providers=config["providers"],
                                custom_providers=[],
                            )
                            if (
                                not result.success
                                or result.target_provider != "openai-api"
                                or result.base_url != gateway_url
                            ):
                                failures.append(
                                    f"{example}: /model {model} resolved to "
                                    f"provider={result.target_provider!r}, "
                                    f"base_url={result.base_url!r}"
                                )
                finally:
                    DIRECT_ALIASES.clear()
                    for name, value in (
                        ("HERMES_HOME", old_home),
                        ("OPENAI_BASE_URL", old_base),
                        ("OPENAI_API_KEY", old_key),
                    ):
                        if value is None:
                            os.environ.pop(name, None)
                        else:
                            os.environ[name] = value

    if failures:
        print("FAIL - Hermes /model can route these GPT models away from openai-api:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("OK - every configured GPT alias uses openai-api through Agentgateway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
