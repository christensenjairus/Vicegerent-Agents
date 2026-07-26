#!/usr/bin/env python3
"""Ensure /model keeps configured GPT aliases on Agentgateway-OpenAI."""

from __future__ import annotations

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
            providers = config.get("providers") or {}
            for alias, spec in (config.get("model_aliases") or {}).items():
                if not isinstance(spec, dict):
                    continue
                provider = spec.get("provider")
                model = spec.get("model")
                if provider != "openai":
                    continue
                declared = (providers.get(provider) or {}).get("models") or []
                if model not in declared:
                    failures.append(
                        f"{example}: model_aliases.{alias} routes {model!r} to "
                        f"{provider!r}, but providers.{provider}.models does not declare it"
                    )

    if failures:
        print("FAIL - Hermes /model can auto-detect these GPT models as OpenRouter:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("OK - every configured GPT alias is declared by Agentgateway-OpenAI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
