#!/usr/bin/env python3
"""Ensure Hermes model switches stay on the configured Agentgateway routes."""

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
            aliases = config.get("model_aliases") or {}

            if "openai-api" in providers:
                if providers["openai-api"].get("transport") != "codex_responses":
                    failures.append(
                        f"{example}: Agentgateway OpenAI transport is not codex_responses"
                    )
                for alias in ("gpt-5", "sol", "terra", "luna"):
                    spec = aliases.get(alias) or {}
                    if spec.get("provider") != "openai-api":
                        failures.append(
                            f"{example}: model_aliases.{alias} does not use openai-api"
                        )
                model_config = config.get("model") or {}
                if (
                    model_config.get("provider") == "openai-api"
                    and model_config.get("api_mode") != "codex_responses"
                ):
                    failures.append(
                        f"{example}: OpenAI primary model does not use codex_responses"
                    )

            for alias, spec in aliases.items():
                if not isinstance(spec, dict):
                    continue
                provider = spec.get("provider")
                model = spec.get("model")
                declared = (providers.get(provider) or {}).get("models") or []
                if provider in {"anthropic", "openai-api"} and model not in declared:
                    failures.append(
                        f"{example}: model_aliases.{alias} routes {model!r} to "
                        f"{provider!r}, but that provider does not declare it"
                    )

            for fallback in config.get("fallback_providers") or []:
                if fallback.get("provider") == "openai":
                    failures.append(
                        f"{example}: OpenAI failover uses the OpenRouter alias 'openai'"
                    )
                expected_mode = {
                    "anthropic": "anthropic_messages",
                    "openai-api": "codex_responses",
                }.get(fallback.get("provider"))
                if expected_mode and fallback.get("api_mode") != expected_mode:
                    failures.append(
                        f"{example}: {fallback.get('provider')} failover does not declare "
                        f"api_mode={expected_mode}"
                    )

            for preset, spec in (config.get("moa") or {}).get("presets", {}).items():
                routes = list(spec.get("reference_models") or []) + [spec.get("aggregator") or {}]
                for route in routes:
                    provider = route.get("provider")
                    if provider and provider not in providers:
                        failures.append(
                            f"{example}: moa.presets.{preset} references undeclared "
                            f"provider {provider!r}"
                        )

        try:
            from agent.auxiliary_client import resolve_provider_client
            from hermes_cli.model_switch import DIRECT_ALIASES, switch_model
        except ImportError:
            switch_model = None

        if switch_model is not None:
            old_env = {
                name: os.environ.get(name)
                for name in (
                    "HERMES_HOME", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                    "OPENAI_API_KEY", "OPENAI_BASE_URL",
                )
            }
            try:
                for index, (example, rendered, config) in enumerate(rendered_configs):
                    providers = config.get("providers") or {}
                    if not {"anthropic", "openai-api"}.issubset(providers):
                        continue
                    home = tmpdir / f"home-{index}"
                    home.mkdir()
                    (home / "config.yaml").write_text(yaml.safe_dump(config))
                    anthropic_env = re.search(
                        r"name:\s*ANTHROPIC_BASE_URL\s*\n\s*value:\s*(\S+)", rendered
                    )
                    if not anthropic_env:
                        failures.append(f"{example}: ANTHROPIC_BASE_URL is not rendered")
                        continue
                    os.environ.update({
                        "HERMES_HOME": str(home),
                        "ANTHROPIC_API_KEY": "none",  # pragma: allowlist secret
                        "ANTHROPIC_BASE_URL": anthropic_env.group(1).strip('"'),
                        "OPENAI_API_KEY": "none",  # pragma: allowlist secret
                        "OPENAI_BASE_URL": providers["openai-api"]["api"],
                    })

                    cases = (
                        ("anthropic", "openai-api", "gpt-5"),
                        ("anthropic", "openai-api", "sol"),
                        ("anthropic", "openai-api", "terra"),
                        ("anthropic", "openai-api", "luna"),
                        ("openai-api", "anthropic", "sonnet"),
                    )
                    for current, expected, requested in cases:
                        DIRECT_ALIASES.clear()
                        result = switch_model(
                            requested,
                            current_provider=current,
                            current_model=(providers[current].get("models") or [""])[0],
                            current_base_url=providers[current]["api"],
                            current_api_key="none",  # pragma: allowlist secret
                            user_providers=providers,
                            custom_providers=[],
                        )
                        if (
                            not result.success
                            or result.target_provider != expected
                            or result.base_url != providers[expected]["api"]
                            or result.api_mode != providers[expected]["transport"]
                        ):
                            failures.append(
                                f"{example}: /model {requested} from {current} resolved to "
                                f"provider={result.target_provider!r}, base_url={result.base_url!r}, "
                                f"api_mode={result.api_mode!r}"
                            )

                    for fallback in config.get("fallback_providers") or []:
                        if fallback.get("provider") != "anthropic":
                            continue
                        client, _ = resolve_provider_client(
                            "anthropic",
                            model=fallback.get("model"),
                            explicit_base_url=fallback.get("base_url"),
                            explicit_api_key="none",  # pragma: allowlist secret
                        )
                        actual = str(getattr(client, "base_url", "")).rstrip("/")
                        expected = providers["anthropic"]["api"].rstrip("/")
                        if actual != expected:
                            failures.append(
                                f"{example}: Anthropic failover client resolved to {actual!r}, "
                                f"expected {expected!r}"
                            )
            finally:
                DIRECT_ALIASES.clear()
                for name, value in old_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    if failures:
        print("FAIL - Hermes model switching can bypass Agentgateway:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("OK - Anthropic and OpenAI model switches stay on Agentgateway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
