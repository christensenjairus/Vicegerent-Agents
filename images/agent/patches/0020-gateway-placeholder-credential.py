#!/usr/bin/env python3
"""Vicegerent patch: accept sandbox placeholder credentials only when the
resolved Anthropic or OpenAI runtime uses the trusted Agentgateway route.

The original patch covered Anthropic's native and credential-pool branches.
OpenAI API uses the same placeholder design but reaches the generic API-key
and pool branches, so both provider paths are kept together here. OpenAI's
normalization is deliberately narrower: it requires the exact in-cluster
Agentgateway OpenAI URL, leaving direct and arbitrary external endpoints
fail-closed.

Context
-------
This platform sets ANTHROPIC_API_KEY="none" in the sandbox env (see  # pragma: allowlist secret
_sandbox.tpl) so Claude Code / Codex / OpenCode / Hermes clients have a non-empty
credential string to send -- the real upstream key is injected by
agentgateway and the value never leaves the pod (egress proxy scrubs it
in transit either way). "none" is intentionally chosen because it IS on
hermes_cli.auth._PLACEHOLDER_SECRET_VALUES: that's what stops the
canonical (non-gateway) "anthropic" / "openai-api" providers from
falsely registering as explicitly user-configured and leaking into the
desktop's Edit Models dialog (see this repo's HAH-112 fix commit).

That fix, applied consistently, surfaces the SAME placeholder rejection
inside hermes_cli.runtime_provider.resolve_runtime_provider(), which has
TWO independent branches that can resolve an "anthropic" runtime and
both need the fix:

1. Pool-entry path (_resolve_runtime_from_pool_entry, the `elif
   provider == "anthropic":` block) -- the one actually exercised on
   this platform. agent/credential_pool.py's _seed_from_env()
   unconditionally seeds ANTHROPIC_API_KEY into the persisted
   credential pool with no placeholder/usability check at all ("none"
   is truthy, so its only guard `if not token: continue` never fires).
   resolve_runtime_provider() checks `if pool and pool.has_credentials():
   ... return _resolve_runtime_from_pool_entry(...)` before anything
   else, so this branch always wins here, and it carries the raw pool
   api_key straight through with no usability check.

2. Fallback native-Anthropic branch (only reached when the pool has no
   usable "anthropic" entry -- doesn't happen on this platform today,
   but would on a differently-configured deployment where the pool is
   empty). Here _anthropic_base_url_override_ok() (patch 0014's helper)
   correctly trusts our gateway base_url and resolve_anthropic_token()
   falls through to os.getenv("ANTHROPIC_API_KEY") = "none" -- non-empty,
   so the branch's own `if not token: raise AuthError(...)` guard does
   not fire, and api_key="none" flows out to callers.  # pragma: allowlist secret

Verified live on the deployed pod: without this patch,
resolve_runtime_provider() returns api_key='none',  # pragma: allowlist secret
source='env:ANTHROPIC_API_KEY' via the pool-entry path (branch 1 above).

Either way, tui_gateway/server.py's "setup.runtime_check" RPC (introduced
to give desktop/dashboard UIs a strict "will this actually work" signal)
wraps that api_key through hermes_cli.auth.has_usable_secret() before
trusting it:

    credential_ok = (
        callable(api_key)
        or api_key_text in {"aws-sdk", "no-key-required"}
        or has_usable_secret(api_key_text)
        or bool(runtime.get("command"))
    )

has_usable_secret("none") is (correctly, per its own contract) False, so
credential_ok is False, and the desktop surfaces "No usable credentials
found for anthropic. setup.status reports configured credentials, but
runtime resolution still failed." -- even though real chat turns work
fine, because the actual Anthropic client request is sent to the trusted
agentgateway base_url, which supplies the real key downstream. This is a
readiness-check false negative, not a functional break.

Fix
---
In BOTH branches: when the request is routed through a trusted gateway
override (only true once _resolve_anthropic_base_url_override() /
_anthropic_base_url_override_ok() -- patch 0014's helpers -- have
already accepted it) AND the resolved token/api_key is not
has_usable_secret() (i.e. it's a recognized placeholder, not a real
secret), normalize it to the same "no-key-required" sentinel this file
already uses elsewhere for local/no-auth servers (Ollama, LM Studio,
etc. -- see _resolve_openrouter_runtime and friends). setup.runtime_check
already explicitly allowlists "no-key-required" as credential_ok, so
this normalization fixes the false negative in whichever branch actually
runs, without touching the gateway/UI code at all.

A genuinely unconfigured Anthropic setup (ANTHROPIC_API_KEY unset, no
OAuth token, no pool entry, no gateway override configured) is
unaffected in both branches: the fallback's `if not token: raise
AuthError(...)` still fires first when there's truly no token, and the
pool-entry branch's gateway-override check only fires when
_resolve_anthropic_base_url_override() actually returns a trusted
override -- otherwise a real key (or lack of one) flows through exactly
as before.

has_usable_secret is already imported at module scope in
runtime_provider.py (patch does not need to add an import).

Fail-loud by design: if either anchor is absent or appears an unexpected
number of times (upstream refactored these branches), the patch raises
and the image build fails, signalling a re-verify.

Depends on patch 0014 (needs cfg_base_url / _anthropic_base_url_override_ok
/ _resolve_anthropic_base_url_override already in place; must run after
it). Once upstream itself normalizes placeholder-shaped-but-gateway-routed
Anthropic tokens (or teaches setup.runtime_check that a trusted gateway
override doesn't need a locally-usable secret), this patch can be
dropped.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0020"
OPENAI_MARKER = "Vicegerent patch 0020 OpenAI"
OPENAI_GATEWAY_URL = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai/v1"

FALLBACK_ANCHOR = (
    "        else:\n"
    "            from agent.anthropic_adapter import resolve_anthropic_token\n"
    "            token = resolve_anthropic_token()\n"
    "            if not token:\n"
    "                raise AuthError(\n"
    "                    \"No Anthropic credentials found. Set ANTHROPIC_TOKEN or ANTHROPIC_API_KEY, \"\n"
    "                    \"run 'claude setup-token', or authenticate with 'claude /login'.\"\n"
    "                )\n"
)

FALLBACK_REPLACEMENT = (
    "        else:\n"
    "            from agent.anthropic_adapter import resolve_anthropic_token\n"
    "            token = resolve_anthropic_token()\n"
    "            if not token:\n"
    "                raise AuthError(\n"
    "                    \"No Anthropic credentials found. Set ANTHROPIC_TOKEN or ANTHROPIC_API_KEY, \"\n"
    "                    \"run 'claude setup-token', or authenticate with 'claude /login'.\"\n"
    "                )\n"
    "            # Vicegerent patch 0020: a trusted gateway override (cfg_base_url\n"
    "            # only ever set once _anthropic_base_url_override_ok() already\n"
    "            # accepted it) supplies the real upstream key downstream, so a\n"
    "            # placeholder-shaped token here (e.g. this platform's\n"
    "            # ANTHROPIC_API_KEY=\"none\") is not a missing credential -- normalize\n"
    "            # it to the same no-key-required sentinel used elsewhere in this\n"
    "            # file for local/no-auth servers, so has_usable_secret() consumers\n"
    "            # (setup.runtime_check) don't report a false readiness failure.\n"
    "            if cfg_base_url and not has_usable_secret(token):\n"
    "                token = \"no-key-required\"\n"
)

POOL_ANCHOR = (
    "    elif provider == \"anthropic\":\n"
    "        api_mode = \"anthropic_messages\"\n"
    "        base_url = (\n"
    "            _resolve_anthropic_base_url_override(model_cfg)\n"
    "            or base_url\n"
    "            or \"https://api.anthropic.com\"\n"
    "        )\n"
)

POOL_REPLACEMENT = (
    "    elif provider == \"anthropic\":\n"
    "        api_mode = \"anthropic_messages\"\n"
    "        _gateway_override = _resolve_anthropic_base_url_override(model_cfg)\n"
    "        base_url = (\n"
    "            _gateway_override\n"
    "            or base_url\n"
    "            or \"https://api.anthropic.com\"\n"
    "        )\n"
    "        # Vicegerent patch 0020: a persisted credential-pool entry can carry\n"
    "        # a placeholder-shaped api_key (this platform's ANTHROPIC_API_KEY=\n"
    "        # \"none\", needed so the canonical anthropic/openai-api providers\n"
    "        # don't falsely register as user-configured) that was seeded into\n"
    "        # the pool with no usability check at all (agent/credential_pool.py's\n"
    "        # _seed_from_env only checks `if not token`, and \"none\" is truthy).\n"
    "        # When a trusted gateway override applies, the real key is supplied\n"
    "        # downstream by the gateway, so normalize such a token to the same\n"
    "        # no-key-required sentinel used elsewhere in this file -- otherwise\n"
    "        # setup.runtime_check's has_usable_secret() check reports a false\n"
    "        # readiness failure even though real requests work fine.\n"
    "        if _gateway_override and not has_usable_secret(api_key):\n"
    "            api_key = \"no-key-required\"\n"
)

OPENAI_HELPER_ANCHOR = (
    'def _loopback_hostname(host: str) -> bool:\n'
    '    h = (host or "").lower().rstrip(".")\n'
    '    return h in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}\n'
)

OPENAI_HELPER_REPLACEMENT = OPENAI_HELPER_ANCHOR + f'''

def _vicegerent_openai_gateway_placeholder_ok(provider: str, base_url: str, api_key: str) -> bool:
    """Trust a placeholder only when Agentgateway supplies the real key."""
    return (
        provider == "openai-api"
        and (base_url or "").rstrip("/") == "{OPENAI_GATEWAY_URL}"
        and not has_usable_secret(api_key)
    )
'''

OPENAI_POOL_ANCHOR = (
    "    # OpenCode base URLs end with /v1 for OpenAI-compatible models, but the\n"
    "    # Anthropic SDK prepends its own /v1/messages to the base_url.  Normalize\n"
)

OPENAI_POOL_REPLACEMENT = (
    '    # Vicegerent patch 0020 OpenAI: the pool seeds OPENAI_API_KEY="none"  # pragma: allowlist secret\n'
    "    # because it checks only truthiness. Agentgateway injects the real key, so\n"
    "    # normalize that placeholder only on this platform's exact trusted route.\n"
    "    if _vicegerent_openai_gateway_placeholder_ok(provider, base_url, api_key):\n"
    '        api_key = "no-key-required"  # pragma: allowlist secret\n'
    "\n"
) + OPENAI_POOL_ANCHOR

OPENAI_API_KEY_ANCHOR = (
    '    if pconfig and pconfig.auth_type == "api_key":\n'
    "        creds = resolve_api_key_provider_credentials(provider)\n"
    "        # An explicitly selected API-key provider is authoritative. Returning\n"
)

OPENAI_API_KEY_REPLACEMENT = (
    '    if pconfig and pconfig.auth_type == "api_key":\n'
    "        creds = resolve_api_key_provider_credentials(provider)\n"
    "        # Vicegerent patch 0020 OpenAI: the non-pool path rejects placeholder-shaped\n"
    "        # keys before constructing the runtime. Normalize only when the same\n"
    "        # credential resolver also selected the exact trusted gateway route.\n"
    "        if _vicegerent_openai_gateway_placeholder_ok(\n"
    '            provider, creds.get("base_url", ""), creds.get("api_key", "")\n'
    "        ):\n"
    "            creds = dict(creds)\n"
    '            creds["api_key"] = "no-key-required"  # pragma: allowlist secret\n'
    "        # An explicitly selected API-key provider is authoritative. Returning\n"
)


def _patch_runtime_provider() -> None:
    spec = importlib.util.find_spec("hermes_cli.runtime_provider")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli/runtime_provider.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    apply_anthropic = APPLIED_MARKER not in src
    apply_openai = OPENAI_MARKER not in src
    if not apply_anthropic and not apply_openai:
        print(f"patch: already applied to {path} - no-op")
        return

    edits = []
    if apply_anthropic:
        edits.extend((
            ("fallback native-Anthropic", FALLBACK_ANCHOR, FALLBACK_REPLACEMENT),
            ("pool-entry anthropic", POOL_ANCHOR, POOL_REPLACEMENT),
        ))
    if apply_openai:
        edits.extend((
            ("OpenAI helper", OPENAI_HELPER_ANCHOR, OPENAI_HELPER_REPLACEMENT),
            ("pool-entry OpenAI", OPENAI_POOL_ANCHOR, OPENAI_POOL_REPLACEMENT),
            ("fallback OpenAI", OPENAI_API_KEY_ANCHOR, OPENAI_API_KEY_REPLACEMENT),
        ))

    for name, anchor, replacement in edits:
        count = src.count(anchor)
        if count != 1:
            raise SystemExit(
                f"patch: expected exactly 1 {name} anchor in {path}, "
                f"found {count} (upstream drifted - re-verify)"
            )
        src = src.replace(anchor, replacement, 1)

    if apply_openai:
        src += f"\n# {OPENAI_MARKER}: gateway-routed placeholders are no-key-required.\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: gateway-routed placeholder tokens now normalize to "
        f"no-key-required ({', '.join(name for name, _, _ in edits)}) in {path}"
    )


def main() -> int:
    _patch_runtime_provider()
    return 0


if __name__ == "__main__":
    sys.exit(main())
