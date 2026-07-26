#!/usr/bin/env python3
"""Vicegerent patch: ONE canonical model-price table, emitted to both price sinks.

Supersedes and replaces three overlapping patches:

  * 0038-glm-usage-pricing.py      (GLM prices + zai billing route)  -- deleted
  * 0043-opus-5-usage-pricing.py   (opus-5 prices)                   -- deleted
  * the price half of 0004-agentburn.py  (agentburn PRICES)          -- 0004 is
    reduced to its HERMES_HOME fix only; its _EXTRA_PRICES moved here.

Why consolidate
---------------
Hermes has TWO independent, unsynchronized price tables, and we were patching
them from three different files with hand-copied numbers:

  agent/usage_pricing.py   _OFFICIAL_DOCS_PRICING  (provider, model) -> PricingEntry
      Drives LIVE session billing: agent.session_estimated_cost_usd,
      session_cost_status, session_model_usage rows, and therefore the Slack
      runtime footer's cost field (patch 0039).

  agentburn/prices.py      PRICES  "vendor/model" -> (in, out)
      Drives the standalone agentburn_burn_report / burn_why MCP tools only.
      Never consulted for live billing.

Measured drift at the time this patch was written (normalizing dots/dashes and
date suffixes before comparing):

  * 12 models priced in agentburn but NOT in usage_pricing -- these show a cost
    in burn_report while the live footer says nothing.
  * 49 models priced in usage_pricing but NOT in agentburn -- the reverse.
  * 17 models duplicated across both, i.e. two hand-maintained copies of the
    same number, and 1 of those 17 had already silently drifted:
    deepseek/deepseek-chat was (0.32, 0.89) in agentburn vs (0.14, 0.28) in
    usage_pricing. usage_pricing is correct -- deepseek-chat was deprecated
    2026-07-24 and now aliases deepseek-v4-flash's rates; agentburn still held
    the pre-deprecation OpenRouter median. That is a 2.3x cost overstatement in
    burn_report, and nothing would ever have caught it.

Two REAL gaps this consolidation surfaced, beyond the opus-5 one it started as
(both are models this repo actually configures in values.defaults.yaml):

  * openai/gpt-5.4 -- `providers.openai.model` AND the `failover.provider:
    openai` target. Completely unpriced in usage_pricing, so every OpenAI
    primary session and every failover session was recording
    cost_status="unknown" and showing no cost in Slack. Strictly a bigger hole
    than the opus-5 one that prompted this work.
  * zai/glm-4.7-flash -- `providers.zai.auxiliaryModel`. Unpriced (0038 added
    glm-5/5.1/5.2 but not the 4.7-flash aux model). Currently latent because
    the zai provider is disabled by default, but it breaks the moment it is
    enabled.

Design
------
_PRICES below is the single source of truth. Each entry is written ONCE and
fanned out to both sinks, so the two tables cannot drift from each other again.

Conflict handling differs per sink, deliberately:

  usage_pricing -- FAIL LOUD. If upstream already defines a key with rates that
      disagree with ours, the build stops. That is the repo's fail-loud patch
      convention, and it doubles as a drift alarm: when a future base-image bump
      finally ships its own claude-opus-5 entry at a rate different from ours,
      the build breaks and we re-verify instead of silently shadowing upstream.
      Identical rates are a no-op, so entries can stay listed here across a base
      bump until they are consciously pruned.

  agentburn -- OVERWRITE. Its PRICES table is self-described as an OpenRouter
      *median-endpoint snapshot*, not official rates, so our official-docs
      numbers are strictly better and disagreement is expected (see the
      deepseek-chat case above). Overwriting is the point.

Both sinks are patched by APPENDING a block at module EOF rather than splicing
into the dict literal. 0038 and the old 0043 both anchored on exact neighboring
dict entries, which is needlessly fragile -- any upstream reordering or reflow
of an unrelated neighbor breaks the anchor. An EOF append only depends on the
module-level names (_OFFICIAL_DOCS_PRICING / PRICES / PricingEntry / Decimal)
still existing, which is a far weaker and more honest coupling. Verified that
lookups still normalize correctly through get_pricing_entry() after an EOF
append (dots-to-dashes, date suffixes, Bedrock region prefixes all still work).

Caveat worth knowing: usage_pricing's "-pro" alias loop for the gpt-5.6 family
runs at its own module scope, ABOVE our appended block. Adding a new base model
here does NOT get an automatic "<model>-pro" alias. List the -pro variant
explicitly if a future family needs one.

Remove once upstream Hermes ships these models in its own snapshots. Each
entry's comment carries its own removal condition where it has one.
"""
import importlib
import importlib.util
import sys
from typing import NamedTuple, Optional

APPLIED_MARKER = "Vicegerent patch 0043"


class P(NamedTuple):
    """One canonical price, in USD per 1M tokens.

    inp/out are required. cache_read/cache_write are optional and are used only
    for the usage_pricing sink -- agentburn's table has no cache dimension.
    burn_slug overrides the agentburn "vendor/model" slug when it differs from
    the usage_pricing (provider, model) key; None means skip the agentburn sink
    entirely (used for Bedrock-style keys agentburn has no concept of).
    """

    provider: str
    model: str
    inp: str
    out: str
    cache_read: Optional[str] = None
    cache_write: Optional[str] = None
    source_url: str = "https://platform.claude.com/docs/en/about-claude/pricing"
    pricing_version: str = "vicegerent-pricing-2026-07"
    burn_slug: Optional[str] = ""  # "" = derive as f"{provider}/{model}"


# ---------------------------------------------------------------------------
# THE canonical table. Add a model here ONCE; both sinks pick it up.
# ---------------------------------------------------------------------------
_ANTHROPIC_DOCS = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI_DOCS = "https://platform.openai.com/docs/pricing"
_ZAI_DOCS = "https://docs.z.ai/guides/overview/pricing"

_PRICES: tuple[P, ...] = (
    # ── Anthropic ────────────────────────────────────────────────────────
    # Opus 5: launched 2026-07-24, same $5/$25 as Opus 4.8 per Anthropic's own
    # launch announcement ("Opus 5 costs the same as Opus 4.8"). Cache rates
    # follow the 10%/125% ratio used by every Opus 4.5-4.8 entry upstream.
    # This is `harnesses.claudeCode` and backs Hermes' `opus` alias.
    P("anthropic", "claude-opus-5", "5.00", "25.00", "0.50", "6.25",
      _ANTHROPIC_DOCS, "anthropic-pricing-2026-07"),
    # Fable 5 -- premium tier, carried over from 0004's agentburn table.
    P("anthropic", "claude-fable-5", "10.00", "50.00", "1.00", "12.50",
      _ANTHROPIC_DOCS, "anthropic-pricing-2026-07"),

    # ── OpenAI ───────────────────────────────────────────────────────────
    # gpt-5.4: `providers.openai.model` and the `failover.provider: openai`
    # target, yet entirely absent from upstream's snapshot -- every OpenAI
    # primary/failover session recorded cost_status="unknown". $2.50/$15.00 is
    # OpenAI's list rate (exactly half of gpt-5.5 on both sides); cached input
    # is the standard 90% discount, matching the gpt-5.6 entries upstream.
    P("openai", "gpt-5.4", "2.50", "15.00", "0.25", None,
      _OPENAI_DOCS, "openai-pricing-2026-07"),
    # gpt-5.5 family, carried over from 0004's agentburn table.
    P("openai", "gpt-5.5", "5.00", "30.00", "0.50", None,
      _OPENAI_DOCS, "openai-pricing-2026-07"),
    P("openai", "gpt-5.5-pro", "30.00", "180.00", None, None,
      _OPENAI_DOCS, "openai-pricing-2026-07"),

    # ── Z.ai (GLM) ───────────────────────────────────────────────────────
    # glm-5/5.1/5.2 were added by 0038; kept here verbatim so deleting 0038
    # loses nothing. glm-4.7-flash is NEW -- it is
    # `providers.zai.auxiliaryModel` and was never priced by 0038. Latent
    # today (zai disabled by default), breaks on enable.
    P("zai", "glm-5.2", "1.40", "4.40", "0.26", None, _ZAI_DOCS, "zai-pricing-2026-07"),
    P("zai", "glm-5.1", "1.40", "4.40", "0.26", None, _ZAI_DOCS, "zai-pricing-2026-07"),
    P("zai", "glm-5", "1.00", "3.20", "0.20", None, _ZAI_DOCS, "zai-pricing-2026-07"),
    P("zai", "glm-4.7-flash", "0.10", "0.60", "0.02", None, _ZAI_DOCS, "zai-pricing-2026-07"),
)

# agentburn-only corrections: models upstream agentburn prices from a stale
# OpenRouter median where usage_pricing already holds the correct official
# rate. Keyed by agentburn slug -> (in, out). Kept separate from _PRICES
# because these must NOT be added to usage_pricing (it is already right).
_BURN_CORRECTIONS: dict[str, tuple[str, str]] = {
    # Deprecated 2026-07-24; now aliases deepseek-v4-flash's rates. agentburn
    # still held the pre-deprecation median (0.32/0.89) = 2.3x overstatement.
    "deepseek/deepseek-chat": ("0.14", "0.28"),
}


# ---------------------------------------------------------------------------
# Sink 1: agent/usage_pricing.py -- live session billing + Slack footer
# ---------------------------------------------------------------------------

# The billing-route branches. 0038 contributed the zai/glm one; the deepseek
# one is NEW and fixes a dead-table bug found by the global audit:
# _OFFICIAL_DOCS_PRICING already ships 4 deepseek entries (deepseek-chat,
# deepseek-reasoner, deepseek-v4-flash, deepseek-v4-pro) but
# resolve_billing_route() had NO deepseek branch, so every deepseek session
# fell through to billing_mode="unknown" and those 4 price entries were
# unreachable — priced on paper, unbillable in practice. Same latent shape as
# the zai gap 0038 fixed. (google/ and bedrock/ entries are likewise
# unreachable, but this repo routes to neither, so they are left alone and
# recorded in the MR's follow-ups instead of speculatively patched.)
_ROUTE_ANCHOR = (
    '    if provider_name in {"minimax", "minimax-cn"}:\n'
    '        return BillingRoute(provider=provider_name, model=model.split("/")[-1], base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
)

_ROUTE_REPLACEMENT = (
    '    if provider_name in {"minimax", "minimax-cn"}:\n'
    '        return BillingRoute(provider=provider_name, model=model.split("/")[-1], base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    "    # Vicegerent patch 0043 (was 0038): route zai/glm to the docs snapshot.\n"
    '    if provider_name in {"zai", "glm"}:\n'
    '        return BillingRoute(provider="zai", model=model.split("/")[-1], base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    "    # Vicegerent patch 0043: deepseek had priced entries but no route.\n"
    '    if provider_name == "deepseek" or base_url_host_matches(base_url or "", "api.deepseek.com"):\n'
    '        return BillingRoute(provider="deepseek", model=model.split("/")[-1], base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
)


def _render_usage_entry(p: P) -> str:
    lines = [
        f'    ("{p.provider}", "{p.model}"): PricingEntry(',
        f'        input_cost_per_million=Decimal("{p.inp}"),',
        f'        output_cost_per_million=Decimal("{p.out}"),',
    ]
    if p.cache_read is not None:
        lines.append(f'        cache_read_cost_per_million=Decimal("{p.cache_read}"),')
    if p.cache_write is not None:
        lines.append(f'        cache_write_cost_per_million=Decimal("{p.cache_write}"),')
    lines += [
        '        source="official_docs_snapshot",',
        f'        source_url="{p.source_url}",',
        f'        pricing_version="{p.pricing_version}",',
        "    ),",
    ]
    return "\n".join(lines)


def _patch_usage_pricing() -> None:
    spec = importlib.util.find_spec("agent.usage_pricing")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/usage_pricing.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch(usage_pricing): already applied to {path} -- no-op")
        return

    # -- billing route for zai/glm -------------------------------------------
    count = src.count(_ROUTE_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(usage_pricing): expected 1 minimax route anchor in {path}, "
            f"found {count} (upstream changed resolve_billing_route -- re-verify)"
        )
    src = src.replace(_ROUTE_ANCHOR, _ROUTE_REPLACEMENT, 1)

    # -- fail loud on any conflict with an existing upstream entry -----------
    # Import the UNPATCHED module to read what upstream already defines, so we
    # never silently shadow a value upstream has since corrected. Compare as
    # Decimal, not string: Decimal("5.00") != Decimal("5") as strings but is
    # the same rate, and a naive string compare would false-alarm the build.
    from decimal import Decimal

    mod = importlib.import_module("agent.usage_pricing")
    existing = getattr(mod, "_OFFICIAL_DOCS_PRICING", {})
    conflicts = []
    for p in _PRICES:
        cur = existing.get((p.provider, p.model))
        if cur is None:
            continue
        differs = (
            cur.input_cost_per_million is not None
            and cur.input_cost_per_million != Decimal(p.inp)
        ) or (
            cur.output_cost_per_million is not None
            and cur.output_cost_per_million != Decimal(p.out)
        )
        if differs:
            conflicts.append(
                f"  {p.provider}/{p.model}: upstream="
                f"${cur.input_cost_per_million}/${cur.output_cost_per_million} "
                f"vicegerent=${p.inp}/${p.out}"
            )
    if conflicts:
        raise SystemExit(
            "patch(usage_pricing): upstream now prices these models DIFFERENTLY "
            "than this patch does. Re-verify against the official rate cards, "
            "then either drop the entry from _PRICES (upstream is right) or "
            "update it (we are right):\n" + "\n".join(conflicts)
        )

    # -- append the price block at EOF ---------------------------------------
    entries = "\n".join(_render_usage_entry(p) for p in _PRICES)
    src += (
        f"\n\n# {APPLIED_MARKER}: canonical vicegerent model prices.\n"
        "# Single source of truth lives in images/hermes/patches/"
        "0043-model-pricing.py;\n"
        "# this block and agentburn/prices.py are both generated from it.\n"
        "_OFFICIAL_DOCS_PRICING.update({\n"
        f"{entries}\n"
        "})\n"
    )

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(
        f"patch(usage_pricing): zai route + {len(_PRICES)} canonical prices "
        f"appended to {path}"
    )


# ---------------------------------------------------------------------------
# Sink 2: agentburn/prices.py -- burn_report / burn_why only
# ---------------------------------------------------------------------------


def _burn_slug(p: P) -> Optional[str]:
    if p.burn_slug is None:
        return None
    return p.burn_slug or f"{p.provider}/{p.model}"


def _patch_agentburn_prices() -> None:
    spec = importlib.util.find_spec("agentburn.prices")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn/prices.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch(agentburn): already applied to {path} -- no-op")
        return

    rows = []
    for p in _PRICES:
        slug = _burn_slug(p)
        if slug is None:
            continue
        rows.append(f'    "{slug}": ({float(p.inp)}, {float(p.out)}),')
    for slug, (i, o) in sorted(_BURN_CORRECTIONS.items()):
        rows.append(f'    "{slug}": ({float(i)}, {float(o)}),  # corrects stale median')

    src += (
        f"\n\n# {APPLIED_MARKER}: canonical vicegerent model prices.\n"
        "# Generated from the same _PRICES table as the agent/usage_pricing.py\n"
        "# block, so the two tables cannot drift. Overwrite (not fail-loud) is\n"
        "# intentional here: upstream PRICES is an OpenRouter median snapshot,\n"
        "# ours are official rate-card figures.\n"
        "PRICES.update({\n" + "\n".join(rows) + "\n})\n"
    )

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(
        f"patch(agentburn): {len(rows)} canonical prices appended to {path}"
    )


def main() -> int:
    _patch_usage_pricing()
    _patch_agentburn_prices()
    return 0


if __name__ == "__main__":
    sys.exit(main())
