# agentgateway (vicegerent patched build)

A source-patched build of the upstream [`agentgateway/agentgateway`](https://github.com/agentgateway/agentgateway) data plane, published to `harbor.hahomelabs.com/vicegerent/agentgateway`. Unlike `images/hermes` (a thin `FROM` derivation of a published image), agentgateway is a Rust binary with no runtime patch seam, so fixes have to be applied to source and compiled. This directory clones upstream at a pinned tag, applies the patches in `patches/`, and builds — reusing upstream's glibc target + arm64 jemalloc page-size handling, dropping the UI feature (the proxy does not serve the dashboard here).

## Why a patched image

A **request-phase** `mcpGuardrails` rejection (how the `mcp-cerbos-shim` denies a Secret read, and how it will deny future destructive actions) is returned by upstream v1.3.1 as **HTTP 400** with the JSON-RPC error in the body (`crates/agentgateway/src/proxy/mod.rs`, the `McpGuardrails` arm of the `ProxyError` status mapping). The Python `mcp` SDK that Hermes uses calls `raise_for_status()` on any non-2xx, which tears down the whole MCP session — so a policy deny surfaces to the agent as "the MCP disconnected" instead of "blocked by policy".

The fix is one line: return that rejection as **HTTP 200** (the JSON-RPC error body is already built correctly downstream — only the status was wrong). 200 is the correct transport for an application-level per-call refusal and matches the gateway's own **response-phase** guardrail path. See `patches/0001-guardrail-reject-200.patch`.

This must be fixed in the **request** phase, not worked around by moving the block to the response phase: response-phase blocking can hide a result but cannot stop a side effect (a destructive command already executed upstream by the time the response exists). Request-phase blocking is the only phase that generalizes to both secret-confidentiality and destructive-action prevention.

Carry this image only until the patches in `patches/` land upstream; an upstream PR returning request-phase guardrail rejections as 200 is filed in parallel. When an upstream release carries all of them, repoint the chart back at the stock image and delete this directory.

## Patches

- `0001-guardrail-reject-200.patch` — `crates/agentgateway/src/proxy/mod.rs`: map the `McpGuardrails` arm to `StatusCode::OK` instead of `BAD_REQUEST` (and updates the corresponding `controller/test/e2e/extmcp_test.go` expectation to 200).
- `0002-promptguard-mask-preserve-content-blocks.patch` — `crates/agentgateway/src/llm/`: mask `promptGuard` text in place instead of round-tripping the message list through `get_messages()`/`set_messages()`. Those two flatten every message to plain text, so a content-block-only turn — most commonly the `tool_result`-only user message that follows every tool call — flattened to an empty string and was written back empty, and Anthropic rejected the request with `messages.N: user messages must have non-empty content`. Adds a `mask_text_content()` trait method that walks each format's own content representation (Anthropic Messages, OpenAI Completions, OpenAI Responses, the realtime `TextRequest`) and masks only genuine text spans, leaving `tool_use`/`tool_result`/images/unknown JSON untouched. Formats with no implementation fall back to a no-op rather than risking corruption. Reject detection never called `set_messages()` and is unaffected.

`git apply --verbose` in the build **hard-fails** if a patch stops applying against a new `AGW_VERSION`, which is the intended signal to re-verify (or drop) the patch rather than silently miscompile.

## Build & push

Built on a machine with internet (your laptop), then pushed to Harbor. The egress-locked cluster only ever pulls.

```sh
docker login harbor.hahomelabs.com
make image PLATFORM=linux/arm64      # Kind on Apple Silicon
make push
# or: make release PLATFORM=linux/arm64 AGW_VERSION=v1.3.1
```

This is a full Rust release compile — the first build is slow; rebuilds reuse the cargo layer cache. `make help` lists targets.

## Version pin & Renovate

`AGW_VERSION` (the upstream tag cloned and built) is tracked by Renovate via the `# renovate: datasource=github-releases depName=agentgateway/agentgateway` comment on the `ARG` in the `Dockerfile`. An upstream release opens an MR bumping it; the build then either still applies the patch cleanly or fails loudly so the patch can be re-checked.

The image `TAG` is `<AGW_VERSION>-rev<N>` (e.g. `v1.3.1-rev2`), the same scheme hermes-agent uses: bump `-rev<N>` on every rebuild that changes what the image contains — `patches/`, the `Dockerfile`, the Rust builder base — and reset to `-rev1` when `AGW_VERSION` bumps. The cluster pulls `IfNotPresent`, so a same-tag rebuild is never redeployed; without the suffix a new patch would silently ship as the old image. Renovate uses an explicit regex versioning rule for this image so the numeric build group orders `rev10` after `rev2` and excludes bare upstream tags. The patched-vs-stock distinction is carried by the **registry path** (`harbor.hahomelabs.com/vicegerent/agentgateway` vs upstream `ghcr.io/agentgateway/agentgateway`).

Keep `AGW_VERSION` in lockstep with the agentgateway chart/data-plane version (`charts/platform/templates/gateway.yaml` `AgentgatewayParameters.spec.image.tag` and the chart pinned in `stages/stages.yaml` / `stages/values/agentgateway.yaml`) when rebuilding.
