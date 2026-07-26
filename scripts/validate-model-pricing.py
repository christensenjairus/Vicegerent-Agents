#!/usr/bin/env python3
"""Assert every model this repo can actually route to has live pricing.

Why this exists
---------------
Model prices live in Hermes' own tables (patched by
images/hermes/patches/0043-model-pricing.py). Nothing previously connected the
models this chart *configures* to the prices Hermes actually *has*, so a model
could be wired up and silently record cost_status="unknown" forever -- the
Slack runtime footer just omits the cost line, which looks like a display quirk
rather than a billing gap. That is exactly how three models were found unpriced
(openai/gpt-5.4 -- the OpenAI primary AND failover target, zai/glm-4.7-flash,
anthropic/claude-opus-5), plus a whole-surface bug where every failover session
was unbillable because the chart hardcoded `provider: custom`.

This check closes that loop: it renders the agent chart across every provider /
failover / mnemosyne permutation, harvests every (provider, model) pair the
rendered output can route to, and asserts each one resolves to a real price
through Hermes' own resolve_billing_route() + get_pricing_entry().

Being priced requires BOTH halves, which is the subtle part:
  1. a price entry exists for (provider, model), and
  2. resolve_billing_route() returns billing_mode != "unknown" for it.
A provider with prices but no route branch is "priced on paper, unbillable in
practice" -- that was true of deepseek (4 entries, no branch) until 0043.

Skipped by design when Hermes' agent.usage_pricing is not importable (i.e.
outside the sandbox image), so this is a no-op on a developer laptop rather
than a false failure. Inside the sandbox and in CI it runs for real.

Usage:  python3 scripts/validate-model-pricing.py [--verbose]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULTS = REPO / "values.defaults.yaml"
EXAMPLE = REPO / "values.example.yaml"
PROVIDERS = ("anthropic", "openai", "deepseek", "zai")

VERBOSE = "--verbose" in sys.argv

# Scratch tree that _load_pricing_module() patches; _load_agentburn_prices()
# reads the second sink out of the same tree.
_SCRATCH: Path | None = None


def _log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"FAIL - command failed: {' '.join(cmd)}\n{proc.stderr[:2000]}"
        )
    return proc.stdout


def _agent_slice(path: Path, expression: str, out: Path) -> Path:
    """Extract agent defaults or a machine agent the way the installer does."""
    out.write_text(_run(["yq", expression, str(path)]))
    return out


def _render(defaults_slice: Path, example_slice: Path, extra: list[str]) -> str:
    return _run(
        [
            "helm", "template", "agent", "charts/agent",
            "-f", str(defaults_slice),
            "-f", str(example_slice),
            *extra,
        ]
    )


def _extract_config_yaml(rendered: str) -> dict:
    """Pull the embedded config.yaml literal block out of the ConfigMap."""
    import yaml

    match = re.search(r"^(\s*)config\.yaml:\s*\|-?\n(.*)", rendered, re.S | re.M)
    if not match:
        raise SystemExit("FAIL - no config.yaml literal block in rendered agent chart")
    block = match.group(2)
    first = next(line for line in block.split("\n") if line.strip())
    indent = len(first) - len(first.lstrip())
    kept: list[str] = []
    for line in block.split("\n"):
        # A non-blank line that is no longer indented means the literal block ended.
        if line.strip() and not line.startswith(" " * indent):
            break
        kept.append(line[indent:] if len(line) >= indent else line)
    return yaml.safe_load("\n".join(kept)) or {}


def _harvest(cfg: dict, rendered: str, scenario: str) -> list[tuple]:
    """Every (provider, model, base_url, site) pair the rendered agent can route to."""
    refs: list[tuple] = []

    def add(provider, model, base_url, site) -> None:
        if model and isinstance(model, str) and provider:
            refs.append((str(provider), model, base_url or "", site, scenario))

    model_cfg = cfg.get("model") or {}
    add(model_cfg.get("provider"), model_cfg.get("default"),
        model_cfg.get("base_url"), "model.default (primary)")

    for alias, spec in (cfg.get("model_aliases") or {}).items():
        if isinstance(spec, dict):
            add(spec.get("provider"), spec.get("model"),
                spec.get("base_url"), f"model_aliases.{alias}")

    for task, spec in (cfg.get("auxiliary") or {}).items():
        if isinstance(spec, dict):
            add(spec.get("provider"), spec.get("model"),
                spec.get("base_url"), f"auxiliary.{task}")

    deleg = cfg.get("delegation") or {}
    add(deleg.get("provider"), deleg.get("model"),
        deleg.get("base_url"), "delegation (subagents)")

    for i, entry in enumerate(cfg.get("fallback_providers") or []):
        if isinstance(entry, dict):
            add(entry.get("provider"), entry.get("model"),
                entry.get("base_url"), f"fallback_providers[{i}] (failover)")

    for provider, spec in (cfg.get("providers") or {}).items():
        for model in ((spec or {}).get("models") or []):
            add(provider, model, (spec or {}).get("api"),
                f"providers.{provider}.models[]")

    # Mnemosyne's model is a pod env var, not part of config.yaml, so it has to
    # be scraped from the rendered Deployment or it stays invisible here.
    model_match = re.search(r"name:\s*MNEMOSYNE_LLM_MODEL\s*\n\s*value:\s*(\S+)", rendered)
    if model_match:
        base_match = re.search(
            r"name:\s*MNEMOSYNE_LLM_BASE_URL\s*\n\s*value:\s*(\S+)", rendered
        )
        provider = scenario.split("mnemosyne=")[-1] if "mnemosyne=" in scenario else None
        if provider in PROVIDERS:
            add(provider, model_match.group(1).strip('"'),
                base_match.group(1).strip('"') if base_match else "",
                "MNEMOSYNE_LLM_MODEL (env)")
    return refs


def _strip_prior_0043(text: str) -> str:
    """Remove an already-applied 0043 block so the patch re-applies cleanly.

    The ambient /opt/hermes tree is usually ALREADY patched (the running image
    baked it in). Copying that as-is makes 0043's idempotency marker short it to
    a no-op, so the check would then assert against whatever the *deployed*
    image happened to contain rather than what this commit produces -- which
    silently hid a regression during development. Strip the appended block (and
    the route branches) to recover a pristine module.
    """
    text = re.split(r"\n\n# Vicegerent patch 0043", text)[0]
    text = re.sub(
        r"[ \t]*# Vicegerent patch 0043[^\n]*\n"
        r"(?:[ \t]*if provider_name[^\n]*\n[ \t]*return BillingRoute[^\n]*\n)?",
        "",
        text,
    )
    return text


def _load_pricing_module():
    """Import Hermes' usage_pricing with THIS REPO's 0043 patch applied.

    Subtlety that matters: the ambient /opt/hermes tree belongs to the image the
    sandbox is *currently running*, which by definition predates any pricing fix
    still sitting unmerged in this worktree. Validating against it would fail a
    correct MR (the fix is in patches/, the running image is a rev behind) and,
    worse, would pass once deployed even if someone later deleted the patch.

    So: copy the pristine module to a scratch tree, apply this repo's
    0043-model-pricing.py to that copy, and validate against the result. That
    asserts what we actually care about -- "the pricing source in this commit
    covers every configured model" -- independent of which image is running.

    Returns (module, note) or (None, reason-to-skip).
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("agent.usage_pricing")
    except Exception as exc:
        return None, f"agent.usage_pricing not importable ({exc.__class__.__name__})"
    if spec is None or not spec.origin:
        return None, "agent.usage_pricing not found (not inside the sandbox image)"

    patch = REPO / "images/hermes/patches/0043-model-pricing.py"
    if not patch.is_file():
        return None, f"{patch.relative_to(REPO)} missing"

    scratch = Path(tempfile.mkdtemp(prefix="model-pricing-check-"))
    pkg = scratch / "agent"
    pkg.mkdir()
    pkg.joinpath("usage_pricing.py").write_text(
        _strip_prior_0043(Path(spec.origin).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text("")
    # 0043 patches agentburn/prices.py as its second sink. Copy the REAL module
    # (not a stub) so the mirrored table it writes can be asserted afterwards --
    # a stub would make the burn_report sink check vacuously pass.
    burn_dst = scratch / "agentburn"
    burn_dst.mkdir()
    (burn_dst / "__init__.py").write_text("")
    try:
        burn_spec = importlib.util.find_spec("agentburn.prices")
    except Exception:
        burn_spec = None
    if burn_spec is not None and burn_spec.origin:
        burn_dst.joinpath("prices.py").write_text(
            _strip_prior_0043(Path(burn_spec.origin).read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        _real_agentburn = True
    else:
        # No agentburn available: give 0043 something importable to write into,
        # and _load_agentburn_prices() will decline to assert against it.
        (burn_dst / "prices.py").write_text(
            "PRICES = {}\n\n\ndef lookup(model):\n    return PRICES.get(model)\n"
        )
        _real_agentburn = False

    proc = subprocess.run(
        [sys.executable, str(patch)],
        cwd=scratch,
        env={**os.environ, "PYTHONPATH": str(scratch)},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            "FAIL - images/hermes/patches/0043-model-pricing.py did not apply "
            f"cleanly to a pristine agent/usage_pricing.py:\n{proc.stdout}\n{proc.stderr}"
        )

    sys.path.insert(0, str(scratch))
    for mod in ("agent", "agent.usage_pricing"):
        sys.modules.pop(mod, None)
    import agent.usage_pricing as patched  # noqa: E402

    origin = getattr(patched, "__file__", None)
    if not origin or str(Path(origin).parent.parent) != str(scratch):
        return None, f"sandbox import shadowed by {origin!r}"
    global _SCRATCH
    _SCRATCH = scratch if _real_agentburn else None
    return patched, "with this repo's 0043 patch applied"


def _load_agentburn_prices():
    """Import agentburn.prices from the same patched scratch tree, if present.

    _load_pricing_module() already ran this repo's 0043 against the scratch
    copy, and 0043 patches BOTH sinks, so the agentburn copy in that tree is
    the post-patch one. Returns the module or None to skip.
    """
    if _SCRATCH is None:
        return None
    try:
        for mod in ("agentburn", "agentburn.prices"):
            sys.modules.pop(mod, None)
        import agentburn.prices as burn  # noqa: E402

        origin = getattr(burn, "__file__", None)
        if not origin or str(Path(origin).parent.parent) != str(_SCRATCH):
            return None
        return burn
    except Exception:
        return None


def main() -> int:
    usage_pricing, note = _load_pricing_module()
    if usage_pricing is None:
        _log(f"SKIP - {note}; this check only runs where Hermes is importable "
             "(sandbox image / CI with the hermes image)")
        return 0
    try:
        import yaml  # noqa: F401
    except Exception:
        _log("SKIP - PyYAML unavailable")
        return 0
    _log(f"INFO - validating {note}")

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        defaults_slice = _agent_slice(DEFAULTS, ".agentDefaults", tmpd / "defaults.yaml")
        example_slice = _agent_slice(EXAMPLE, ".agents[0]", tmpd / "example.yaml")

        all_on = [
            f"--set=providers.{p}.enabled=true" for p in PROVIDERS
        ]

        scenarios: dict[str, list[str]] = {"as_shipped": []}
        # Every failover target and every mnemosyne provider, with all providers
        # enabled -- otherwise a disabled provider hides its models from the audit.
        for provider in PROVIDERS:
            scenarios[f"failover={provider}"] = [
                *all_on, f"--set=failover.provider={provider}"
            ]
            scenarios[f"mnemosyne={provider}"] = [
                *all_on,
                "--set=failover.provider=openai",
                f"--set=mnemosyne.provider={provider}",
            ]

        refs: list[tuple] = []
        for scenario, extra in scenarios.items():
            rendered = _render(defaults_slice, example_slice, extra)
            refs.extend(_harvest(_extract_config_yaml(rendered), rendered, scenario))

    if not refs:
        raise SystemExit("FAIL - harvested zero model references; the harvester is broken")

    pairs = sorted({(p, m, b) for p, m, b, _s, _sc in refs}, key=lambda x: (x[0], x[1]))
    unpriced: list[tuple[str, str, str, list[str]]] = []
    for provider, model, base_url in pairs:
        route = usage_pricing.resolve_billing_route(
            model, provider=provider, base_url=base_url or None
        )
        entry = usage_pricing.get_pricing_entry(
            model, provider=provider, base_url=base_url or None
        )
        has_price = entry is not None and entry.input_cost_per_million is not None
        routable = route.billing_mode != "unknown"
        sites = sorted({s for p, m, _b, s, _sc in refs if (p, m) == (provider, model)})
        if has_price and routable:
            if VERBOSE and entry is not None:
                _log(f"  ok   {provider}/{model} -> "
                     f"${entry.input_cost_per_million}/${entry.output_cost_per_million}")
            continue
        why = []
        if not has_price:
            why.append("no price entry")
        if not routable:
            why.append('billing_mode="unknown" (no resolve_billing_route branch)')
        unpriced.append((provider, model, "; ".join(why), sites))

    if unpriced:
        _log("FAIL - these configured models would record cost_status=\"unknown\" "
             "and show NO cost in the Slack footer:\n")
        for provider, model, why, sites in unpriced:
            _log(f"  {provider}/{model}")
            _log(f"      why:   {why}")
            _log(f"      used:  {', '.join(sites)}")
        _log("\nFix by adding the model to _PRICES (and, if the provider has no "
             "route branch, a branch too) in images/hermes/patches/0043-model-pricing.py")
        return 1

    scenario_count = len({r[4] for r in refs})
    _log(f"OK - {len(pairs)} configured (provider, model) routes across "
         f"{scenario_count} scenarios all have live pricing")

    # Second sink: agentburn/prices.py drives burn_report/burn_why. It is a
    # SEPARATE table from usage_pricing, so a model can bill correctly live and
    # still be invisible to burn_report -- which is exactly what happened when
    # an earlier cut of 0043 hand-listed this sink and silently dropped 13
    # models (claude-haiku-4-5, gpt-5.6-sol, ...) that the retired 0004 used to
    # cover. Live billing stayed green, so only checking usage_pricing above
    # would have missed it entirely.
    burn = _load_agentburn_prices()
    if burn is None:
        _log("INFO - agentburn.prices not importable; skipping burn_report sink check")
        return 0
    missing = []
    for provider, model, _base in pairs:
        if burn.lookup(f"{provider}/{model}") is None:
            missing.append(f"{provider}/{model}")
    if missing:
        _log("\nFAIL - these configured models have live pricing but are INVISIBLE to "
             "agentburn burn_report (cost_known=false):\n")
        for slug in sorted(set(missing)):
            _log(f"  {slug}")
        _log("\nThe agentburn sink in images/hermes/patches/0043-model-pricing.py is "
             "derived from the post-patch usage_pricing table; if a model is missing "
             "here but present above, that mirroring broke.")
        return 1
    _log(f"OK - all {len(pairs)} routes are also priced in the agentburn "
         "burn_report sink")
    return 0


if __name__ == "__main__":
    sys.exit(main())
