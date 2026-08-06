#!/usr/bin/env python3
"""Ensure every rendered Hermes model route is locked to Agentgateway."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
GATEWAY = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local"
PROVIDERS = {
    "anthropic": {
        "id": "anthropic", "api": f"{GATEWAY}/anthropic",
        "key": "ANTHROPIC_API_KEY", "base_env": "ANTHROPIC_BASE_URL",
        "mode": "anthropic_messages",
    },
    "openai": {
        "id": "openai-api", "api": f"{GATEWAY}/openai/v1",
        "key": "OPENAI_API_KEY", "base_env": "OPENAI_BASE_URL",
        "mode": "codex_responses",
    },
    "deepseek": {
        "id": "deepseek", "api": f"{GATEWAY}/deepseek/v1",
        "key": "DEEPSEEK_API_KEY", "base_env": "DEEPSEEK_BASE_URL",
        "mode": "chat_completions",
    },
    "zai": {
        "id": "zai", "api": f"{GATEWAY}/zai/api/paas/v4",
        "key": "ZAI_API_KEY", "base_env": "ZAI_BASE_URL",
        "mode": "chat_completions",
    },
}
BY_ID = {spec["id"]: spec for spec in PROVIDERS.values()}


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    if check and proc.returncode:
        raise SystemExit(f"FAIL - {' '.join(args)}\n{proc.stderr}")
    return proc


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


def render(defaults: Path, agent: dict, destination: Path) -> tuple[str, dict]:
    destination.write_text(yaml.safe_dump(agent))
    output = command(
        "helm", "template", "agent", "charts/agent",
        "-f", str(defaults), "-f", str(destination),
    ).stdout
    return output, extract_config(output)


def assert_route(
    label: str, route: dict, providers: dict, failures: list[str], *, connection: bool = True
) -> None:
    provider_id = route.get("provider")
    if provider_id not in providers:
        failures.append(f"{label}: references undeclared provider {provider_id!r}")
        return
    expected = BY_ID.get(provider_id)
    if not expected:
        failures.append(f"{label}: provider {provider_id!r} is not canonical")
        return
    fields = [("base_url", expected["api"])]
    if connection:
        fields.extend((("key_env", expected["key"]), ("api_mode", expected["mode"])))
    for field, value in fields:
        if route.get(field) != value:
            failures.append(f"{label}.{field}: got {route.get(field)!r}, expected {value!r}")
    model = route.get("model") or route.get("default")
    if model and model not in (providers[provider_id].get("models") or []):
        failures.append(f"{label}: model {model!r} is not declared by provider {provider_id!r}")


def inspect(label: str, rendered: str, config: dict, enabled: set[str], failures: list[str]) -> None:
    providers = config.get("providers") or {}
    if (config.get("kanban") or {}).get("dispatch_in_gateway") is not True:
        failures.append(f"{label}: Kanban gateway dispatcher is not enabled")
    expected_ids = {PROVIDERS[name]["id"] for name in enabled}
    if set(providers) != expected_ids:
        failures.append(f"{label}: provider IDs are {sorted(providers)}, expected {sorted(expected_ids)}")
    for name in enabled:
        expected = PROVIDERS[name]
        actual = providers.get(expected["id"]) or {}
        for field, value in (
            ("api", expected["api"]), ("key_env", expected["key"]),
            ("transport", expected["mode"]),
        ):
            if actual.get(field) != value:
                failures.append(f"{label}: providers.{expected['id']}.{field} is not canonical")
        env_match = re.search(
            rf"name:\s*{expected['base_env']}\s*\n\s*value:\s*([^\s]+)", rendered
        )
        if not env_match or env_match.group(1).strip("'\"") != expected["api"]:
            failures.append(f"{label}: {expected['base_env']} is not the canonical gateway URL")

    for provider_id, spec in providers.items():
        api = str(spec.get("api", ""))
        if not api.startswith(f"{GATEWAY}/"):
            failures.append(f"{label}: providers.{provider_id}.api bypasses Agentgateway: {api!r}")

    assert_route(f"{label}.model", config.get("model") or {}, providers, failures)
    for alias, route in (config.get("model_aliases") or {}).items():
        assert_route(
            f"{label}.model_aliases.{alias}", route, providers, failures, connection=False
        )
    moa = config.get("moa") or {}
    presets = moa.get("presets") or {}
    if moa.get("default_preset") != "default" or set(presets) != {"default", "frontier"}:
        failures.append(f"{label}.moa: presets must be exactly default and frontier")
    for preset, spec in presets.items():
        for index, route in enumerate(spec.get("reference_models") or []):
            provider_id = route.get("provider")
            if provider_id not in providers:
                failures.append(f"{label}.moa.{preset}.reference_models[{index}]: undeclared provider")
            elif route.get("model") not in (providers[provider_id].get("models") or []):
                failures.append(f"{label}.moa.{preset}.reference_models[{index}]: undeclared model")
        aggregator = spec.get("aggregator") or {}
        if aggregator.get("provider") not in providers:
            failures.append(f"{label}.moa.{preset}.aggregator: undeclared provider")
        elif aggregator.get("model") not in (
            providers[aggregator["provider"]].get("models") or []
        ):
            failures.append(f"{label}.moa.{preset}.aggregator: undeclared model")
    for index, route in enumerate(config.get("fallback_providers") or []):
        assert_route(f"{label}.fallback_providers[{index}]", route, providers, failures)
    assert_route(f"{label}.delegation", config.get("delegation") or {}, providers, failures)
    for name, route in (config.get("auxiliary") or {}).items():
        assert_route(f"{label}.auxiliary.{name}", route, providers, failures)

    serialized = yaml.safe_dump(config)
    for forbidden in ("api.openai.com", "api.anthropic.com", "openrouter.ai", "https://evil.example"):
        if forbidden in serialized:
            failures.append(f"{label}: hostile upstream URL survived rendering: {forbidden}")


def main() -> int:
    failures: list[str] = []
    rendered_configs: list[tuple[str, str, dict]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        defaults = tmpdir / "defaults.yaml"
        defaults.write_text(command("yq", ".agentDefaults", "values.defaults.yaml").stdout)
        default_values = yaml.safe_load(defaults.read_text())

        scenarios: list[tuple[str, dict, set[str]]] = []
        for name, provider in PROVIDERS.items():
            enabled = {candidate: {"enabled": candidate == name} for candidate in PROVIDERS}
            scenarios.append((
                f"only-{name}",
                {
                    "providers": enabled,
                    "failover": {"provider": ""},
                    "mnemosyne": {"provider": name},
                    "moa": {"aggregator": {
                        "provider": name,
                        "models": {
                            "default": default_values["providers"][name]["moaModels"]["balanced"],
                            "frontier": default_values["providers"][name]["moaModels"]["frontier"],
                        },
                    }},
                },
                {name},
            ))
        for primary_name, primary in PROVIDERS.items():
            for fallback_name in PROVIDERS:
                scenarios.append((
                    f"{primary_name}-to-{fallback_name}",
                    {
                        "providers": {name: {"enabled": True} for name in PROVIDERS},
                        "failover": {"provider": fallback_name, "model": ""},
                        "config": {
                            "model": {
                                "provider": primary["id"],
                                "default": default_values["providers"][primary_name]["model"],
                            },
                            "delegation": {
                                "provider": primary["id"],
                                "model": default_values["providers"][primary_name]["model"],
                            },
                            "auxiliary": {"vision": {
                                "provider": primary["id"],
                                "model": default_values["providers"][primary_name]["auxiliaryModel"],
                            }},
                        },
                    },
                    set(PROVIDERS),
                ))

        scenarios.append((
            "hostile-overrides",
            {
                "providers": {name: {"enabled": True} for name in PROVIDERS},
                "failover": {"provider": "anthropic", "model": ""},
                "config": {
                    "providers": {"evil": {"api": "https://evil.example"}},
                    "model_aliases": {"evil": {"provider": "evil", "base_url": "https://evil.example"}},
                    "moa": {"presets": {"balanced": {"aggregator": {"provider": "evil"}}}},
                    "fallback_providers": [{"provider": "evil", "base_url": "https://evil.example"}],
                    "model": {
                        "provider": "openai-api", "default": "gpt-5.4",
                        "base_url": "https://api.openai.com",
                    },
                    "delegation": {"provider": "anthropic", "base_url": "https://api.anthropic.com"},
                    "auxiliary": {"vision": {
                        "provider": "openai-api", "model": "gpt-4o-mini",
                        "base_url": "https://api.openai.com",
                    }},
                },
            },
            set(PROVIDERS),
        ))

        profile_aggregators = {
            "values.example.yaml": ("anthropic", "claude-sonnet-5", "claude-opus-5"),
            "examples/personal.yaml": ("openai-api", "gpt-5.6-terra", "gpt-5.6-sol"),
            "examples/work.yaml": ("openai-api", "gpt-5.6-terra", "gpt-5.6-sol"),
        }
        profile_enabled = {
            "values.example.yaml": {"anthropic"},
            "examples/personal.yaml": set(PROVIDERS),
            "examples/work.yaml": {"anthropic", "openai"},
        }
        for profile, (provider_id, default_model, frontier_model) in profile_aggregators.items():
            agent = yaml.safe_load((REPO / profile).read_text())["agents"][0]
            label = f"profile-{Path(profile).stem}"
            rendered, config = render(defaults, agent, tmpdir / f"{label}.yaml")
            inspect(label, rendered, config, profile_enabled[profile], failures)
            presets = config["moa"]["presets"]
            actual = (
                presets["default"]["aggregator"]["provider"],
                presets["default"]["aggregator"]["model"],
                presets["frontier"]["aggregator"]["model"],
            )
            if actual != (provider_id, default_model, frontier_model):
                failures.append(f"{label}: MoA aggregators are {actual!r}")
            rendered_configs.append((label, rendered, config))

        for index, (label, agent, enabled) in enumerate(scenarios):
            rendered, config = render(defaults, agent, tmpdir / f"agent-{index}.yaml")
            inspect(label, rendered, config, enabled, failures)
            expected_reasoning = {
                default_values["providers"][name]["model"]:
                    default_values["providers"][name]["reasoningEffort"]
                for name in enabled
            }
            actual_reasoning = (config.get("agent") or {}).get("reasoning_overrides") or {}
            if actual_reasoning != expected_reasoning:
                failures.append(
                    f"{label}: reasoning overrides are {actual_reasoning!r}, "
                    f"expected {expected_reasoning!r}"
                )
            rendered_configs.append((label, rendered, config))

        custom_efforts = {
            "anthropic": "low", "openai": "high", "deepseek": "medium", "zai": "max",
        }
        custom_reasoning_agent = {
            "providers": {
                name: {"enabled": True, "reasoningEffort": effort}
                for name, effort in custom_efforts.items()
            },
            "tuning": {"reasoningEffort": "none"},
        }
        _, custom_reasoning_config = render(
            defaults, custom_reasoning_agent, tmpdir / "custom-reasoning.yaml"
        )
        expected_reasoning = {
            default_values["providers"][name]["model"]: effort
            for name, effort in custom_efforts.items()
        }
        custom_agent_config = custom_reasoning_config.get("agent") or {}
        if custom_agent_config.get("reasoning_effort") != "none":
            failures.append("custom-reasoning: global reasoning fallback was not preserved")
        if custom_agent_config.get("reasoning_overrides") != expected_reasoning:
            failures.append(
                "custom-reasoning: provider-specific reasoning efforts were not preserved"
            )

        invalid = (
            ("unknown-failover", {"failover": {"provider": "evil"}}),
            ("disabled-failover", {"providers": {"openai": {"enabled": False}}, "failover": {"provider": "openai"}}),
            ("custom-provider", {"config": {"custom_providers": [{"name": "evil", "base_url": "https://evil.example"}]}}),
            ("unknown-primary", {"config": {"model": {"provider": "evil"}}}),
            ("unknown-aggregator", {"moa": {"aggregator": {"provider": "evil"}}}),
            ("disabled-aggregator", {
                "providers": {"anthropic": {"enabled": False}},
                "moa": {"aggregator": {"provider": "anthropic"}},
            }),
            ("scalar-delegation", {"config": {"delegation": "https://api.openai.com"}}),
            ("scalar-auxiliary", {"config": {"auxiliary": "https://api.openai.com"}}),
            ("scalar-auxiliary-route", {"config": {"auxiliary": {"vision": "https://api.openai.com"}}}),
        )
        for label, agent in invalid:
            path = tmpdir / f"invalid-{label}.yaml"
            path.write_text(yaml.safe_dump(agent))
            proc = command(
                "helm", "template", "agent", "charts/agent",
                "-f", str(defaults), "-f", str(path), check=False,
            )
            if proc.returncode == 0:
                failures.append(f"{label}: unsafe configuration rendered successfully")

        try:
            from agent.auxiliary_client import resolve_provider_client
            from hermes_cli.model_switch import DIRECT_ALIASES, switch_model
        except ImportError:
            switch_model = None

        if switch_model is not None:
            old_env = {name: os.environ.get(name) for name in (
                "HERMES_HOME", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL",
            )}
            try:
                label, _, config = next(
                    item for item in rendered_configs if item[0] == "hostile-overrides"
                )
                providers = config["providers"]
                home = tmpdir / "hermes-home"
                home.mkdir()
                (home / "config.yaml").write_text(yaml.safe_dump(config))
                os.environ.update({
                    "HERMES_HOME": str(home),
                    "ANTHROPIC_API_KEY": "none",  # pragma: allowlist secret
                    "ANTHROPIC_BASE_URL": PROVIDERS["anthropic"]["api"],
                    "OPENAI_API_KEY": "none",  # pragma: allowlist secret
                    "OPENAI_BASE_URL": PROVIDERS["openai"]["api"],
                })
                for current, expected, requested in (
                    ("anthropic", "openai-api", "gpt-5"),
                    ("anthropic", "openai-api", "sol"),
                    ("anthropic", "openai-api", "terra"),
                    ("anthropic", "openai-api", "luna"),
                    ("openai-api", "anthropic", "sonnet"),
                ):
                    DIRECT_ALIASES.clear()
                    result = switch_model(
                        requested, current_provider=current,
                        current_model=providers[current]["models"][0],
                        current_base_url=providers[current]["api"], current_api_key="none",
                        user_providers=providers, custom_providers=[],
                    )
                    target = providers[expected]
                    if not result.success or (
                        result.target_provider, result.base_url, result.api_mode
                    ) != (expected, target["api"], target["transport"]):
                        failures.append(f"{label}: /model {requested} from {current} escaped Agentgateway")

                fallback = config["fallback_providers"][0]
                client, _ = resolve_provider_client(
                    "anthropic", model=fallback["model"],
                    explicit_base_url=fallback["base_url"], explicit_api_key="none",
                )
                actual = str(getattr(client, "base_url", "")).rstrip("/")
                expected = PROVIDERS["anthropic"]["api"].rstrip("/")
                if actual != expected:
                    failures.append(
                        f"{label}: Anthropic fallback client used {actual!r}, expected {expected!r}"
                    )
            finally:
                DIRECT_ALIASES.clear()
                for name, value in old_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    if failures:
        print("FAIL - Hermes model routing is not locked to Agentgateway:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(
        f"OK - {len(scenarios)} provider/failover configurations and "
        f"{len(profile_aggregators)} profiles stay on Agentgateway"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
