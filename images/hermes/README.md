# hermes-agent (vicegerent derived image)

A thin derivation of the upstream [`nousresearch/hermes-agent`](https://hub.docker.com/r/nousresearch/hermes-agent) image, published to `harbor.hahomelabs.com/vicegerent/hermes-agent`. The sandbox is egress-locked and cannot reach Docker Hub, npm, or PyPI at runtime (`HERMES_DISABLE_LAZY_INSTALLS=1` is baked into the upstream image), so anything the agent needs must be present in the image. This is the base every bake builds on.

## Why a derived image

The stock image ships the Hermes runtime but **not** the pieces this platform relies on. Verified against the upstream arm64 image (`v2026.7.20`):

| Needed | In stock image? |
| --- | --- |
| mnemosyne plugin + MiniCPM embedding gguf | no |
| hermes-lcm context engine | no |
| LSP servers (pyright, yaml-language-server, terraform-ls, bash-language-server) | no |
| `terragrunt` for HCL/Terraform formatting hooks | no |
| netdebug tools (ss, dig, nc) for egress diagnostics | no |
| bazel / bazelisk / buildozer / buildifier | no |

## Build & push

Built on a machine with internet (your laptop), then pushed to Harbor. The egress-locked cluster only ever pulls.

```sh
docker login harbor.hahomelabs.com
make image PLATFORM=linux/arm64      # Kind on Apple Silicon
make push
# or: make release PLATFORM=linux/arm64 TAG=v2026.7.20-rev6
```

`make help` lists targets. `TAG` is `<upstream-version>-rev<N>` (e.g. `v2026.7.20-rev1`): keep the base in sync with the `FROM` upstream version and bump `-rev<N>` on every rebuild that changes what the image contains, resetting to `-rev1` when the upstream base bumps. The cluster pulls `IfNotPresent`, so a same-tag rebuild is never redeployed — see the image-tag-bump rule in `AGENTS.md`.

## Base pin

`FROM` is pinned by **tag + digest**. The tag keeps the reference Renovate-trackable (an upstream bump opens an MR); the digest makes the build reproducible. The agent `Sandbox` (rendered by `charts/agent`) is repointed at this Harbor image via `values.defaults.yaml`'s `agents[0].image`, tracked by Renovate's `custom.regex` manager in `renovate.json`.

## Bakes

- mnemosyne + MiniCPM `MiniCPM5-1B-Q4_K_M.gguf` (bake outside `/opt/data`; the data PVC shadows `/opt/data` at runtime, so first-boot seeding is an init-container concern, not a Dockerfile one).
- hermes-lcm context engine — extracted from its pinned GitHub release into Hermes' bundled `plugins/context_engine/lcm/` (resolved from the installed package, not a hardcoded path). Loaded by the dedicated context-engine discovery, not the general `~/.hermes/plugins` system, so `hermes plugins install` does not place it; selected via `context.engine: lcm` in the agent config. Pure stdlib, no PyPI deps.
- LSP servers via `npm -g` (node + npm are in the base).
- `terragrunt` via pinned GitHub release asset, for repos whose pre-commit or CI uses `terragrunt hcl format` against `.tf`/`.hcl` files.
- `buf` via GitHub release tarball -- `buf lint`/`buf format` for repos whose pre-commit runs those as actual hooks.
- `clang-format` via `pip install` (PyPI's `clang-format` package ships a prebuilt binary wheel with a console-script entry point) -- lands in `/opt/hermes/.venv/bin` alongside the other `requirements.txt` tools, no separate bake step needed.
- netdebug tools (`iproute2`/`ss`, `dnsutils`/`dig`, `netcat-openbsd`/`nc`) via apt, for diagnosing egress / CiliumNetworkPolicy hangs from inside the sandbox. A default-deny policy DROPS a blocked outbound connect, so it sits in SYN-SENT until timeout — `ss -tanp state syn-sent` names the stuck dest + PID with no added capabilities. Deliberately excludes `strace`/`tcpdump`: those need `CAP_SYS_PTRACE`/`CAP_NET_RAW`, which the locked-down securityContext strips, so they would bake fine but fail to attach at runtime.
- `yq` + `jq` + `pygount`; `rtk-hermes` plugin.
- `bazelisk` (symlinked as `bazel`) + `buildifier`/`buildozer` — bazelisk's cache is pre-warmed at build time against the pinned `BAZEL_PINNED_VERSION` (kept in sync with the `.bazelversion` of the Bazel repos this sandbox operates against — moveworks + k8s-manifests: `8.7.0`), baked directly into `BAZELISK_HOME` (`/opt/hermes/.cache/bazelisk`). That path is read-only at runtime, which is fine: bazelisk needs zero writes for a cache hit on the pinned version (verified — a fully read-only `BAZELISK_HOME` still resolves `bazelisk version` for `BAZEL_PINNED_VERSION` with no network call), so no PVC seeding step is needed. `bazelisk`/`buildifier`/`buildozer` binaries come from GitHub release assets; the pinned `bazel(1)` release itself comes from `releases.bazel.build`, not GitHub, and is **not** on the egress allowlist by design — same offline-only convention as every other pinned tool in this Dockerfile (buildifier/buildozer/helm/rtk/terragrunt included). A `bazel` invocation for any version other than `BAZEL_PINNED_VERSION` will fail at runtime (no write access, no egress) — bump `BAZEL_PINNED_VERSION` and rebuild instead of trying to fetch a different version live. This does not solve Bzlmod dependency resolution (`bcr.bazel.build`) for repos that pull external modules — a Bzlmod-using target still needs either a Bazel Central Registry egress allowlist entry or a pre-seeded `MODULE.bazel` dependency cache; this bake only guarantees the `bazel`/`buildozer`/`buildifier` binaries themselves are present and the pinned Bazel version runs offline. Note: `BUILDIFIER_VERSION`/`BUILDOZER_VERSION` here are bumped independently of any one target repo's own pinned linter version (e.g. a target repo's `tools/onboarding/versions.sh` pins `8.2.1` and its `linter_verify` pre-commit hook does an exact-string version check) — a mismatch there fails that repo's own pre-commit hook on version grounds even when formatting itself is correct. Not worked around here; this sandbox doesn't commit into that repo's worktree.
- `absl-py` (`absl`), `pygithub` (`github`), `toml`, `validators`, `yamllint` via `requirements.txt` — an internal monorepo's own `tools/ci/*` and `tools/precommit_hooks/*` entry points declare their local hooks `language: system` in `.pre-commit-config.yaml` (i.e. run against whatever `python` is on `PATH`, not an isolated pre-commit-managed venv) — so they must be importable from the hermes venv directly, not installable at runtime (`/opt/hermes/.venv` is read-only outside the build). `toml` is load-bearing for that repo's `tools.ci.*` entry points via `tools/common/repo_utils.py`; `absl`/`pygithub`/`validators` are used by `tools/base`, `tools/utils/bcdr`, and `tools/fix_builds` respectively; `yamllint` backs the `yamllint` pre-commit hook itself (`python -m yamllint`). Confirmed by actually running that repo's `tools.ci.ci_lint` end-to-end after adding these — the only remaining pre-commit dependency, `ruff`, already works unmodified: it's a hosted (non-`local`) pre-commit repo, so pre-commit manages its own isolated env for it on first run, independent of this venv.

## Patches

Upstream Hermes is also customized at build time by numbered Python scripts in `patches/`, each `COPY`d in, run against `/opt/hermes/.venv`, then deleted. They edit installed package files in place and self-verify where feasible; remove one once the fix lands upstream. Numbering is sparse and non-contiguous — a patch is dropped once its fix lands upstream and new ones are appended, so the sequence has gaps; `patches/` itself is the authoritative list. The entries below cover a curated subset of the notable patches, not every file in the directory.

- `0004-agentburn.py` — `HERMES_HOME` support for the agentburn adapter and missing Anthropic/OpenAI model prices.
- `0007-slack-bypass-egress-proxy.py` — patch `slack_sdk`'s env proxy loader to return `None` so Slack bypasses the GET-only egress MITM proxy (`slack_sdk` ignores `NO_PROXY`, which would otherwise force every Slack call through the proxy and fail).
- `0008-approval-tirith-only-mode.py` — add `approvals.pattern_silence` to smart-mode command approval so operator-configured false-positive patterns skip the aux-LLM pre-screen (tirith findings and uncancellable patterns are never silenced).
- `0009-mcp-circuit-breaker.py` — stop the MCP circuit breaker from tripping on business errors (`isError: true` relayed as a JSON `"error"` key); only real transport/auth exceptions should count toward the 3-strike "server unreachable" block. Remove once upstream lands hermes-agent #47918/#47955 (issues #47851/#11113).
- `0011-web-extract-capability-check.py` — gate `web_extract` on an extract-capable backend (firecrawl/tavily/exa/parallel) rather than the shared "any web backend configured" check. We run SearXNG-only (`web.search_backend: searxng`); SearXNG passes the shared check but can't extract, so `web_extract` was registering and erroring at call time instead of simply not existing. Remove once upstream gates `web_extract` on `supports_extract()` itself (loosely tracked with hermes-agent #19198).
- `0012-execute-code-pattern-silence.py` — let `approvals.pattern_silence` also silence `execute_code`'s separate whole-script approval gate (`check_execute_code_guard`), which previously ignored the silence list entirely (only `check_all_command_guards`, the regex-flagged `terminal()` path, consulted it). Arbitrary script execution is this sandbox's intended capability — the isolation boundary is the pod/network layer (no Docker socket, egress-locked, non-root), not this gate. Silenced via `execute_code` in `approval-policy.yaml`'s `pattern_silence` list. Remove once upstream lets a single silence list cover both approval paths.
- `0014-auxiliary-anthropic-gateway-host.py` — trust our in-cluster agentgateway `/anthropic` proxy route for Anthropic traffic in both the auxiliary client (`agent/auxiliary_client.py`) and the main/subagent runtime resolver (`hermes_cli/runtime_provider.py`); also de-duplicates the resolver's override-check logic that was copy-pasted across its three Anthropic resolution call sites (credential-pool, explicit-override, env-var/fallback), so a future edit to one copy can't silently desync it from the other two. Remove once upstream unifies the auxiliary-client and main-client Anthropic-host trust logic and de-duplicates the three call sites itself.
- `0016-credential-pool-anthropic-base-url.py` — stop the persisted Anthropic credential-pool entry (`agent/credential_pool.py`) from silently reintroducing a raw `api.anthropic.com` host that 0014 already fixed at the resolution layer: `_seed_from_env()` now consults the same `_resolve_anthropic_base_url_override()` helper before falling back to a hardcoded default, and `run_agent.py`'s `_swap_credential()` (leased by every `delegate_task()` subagent before its first call) re-derives the base_url through that helper on every lease, self-healing already-stale persisted entries. Depends on 0014. Remove once the credential pool itself understands gateway-style Anthropic base_url overrides.
- `0040-custom-provider-drop-unconditional-think-field.py` — stop the `custom` provider profile (`plugins/model-providers/custom`) from unconditionally emitting `extra_body.think = False` when reasoning is disabled. The OpenAI SDK flattens `extra_body` into a bare top-level `think` key on the wire, which schema-strict OpenAI-compatible endpoints (real OpenAI, and agentgateway passing it straight through — e.g. this cluster's `gpt-5.4` fallback route) reject with `HTTP 400: Unknown parameter: 'think'`; the field was already a no-op for its stated Ollama purpose on the `/v1/chat/completions` transport Hermes actually uses.
- `0041-interim-assistant-messages-session-toggle.py` — add a native `/chatter` command (Slack bang-alias `!chatter` via the existing `!`→`/` rewrite) that toggles `display.interim_assistant_messages` for the current session only — in-memory, never persisted to config.yaml, resets at conversation boundaries like `/reasoning`'s session override. Adds wholly new functionality; no upstream removal condition.
