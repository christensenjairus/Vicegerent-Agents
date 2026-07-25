# Vicegerent Agents

Repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked agent sandboxes on a local Kind cluster (Cilium CNI), installed by a staged Helm script. The platform is **harness-agnostic**: the same sandbox runs whichever coding agent you point it at — Hermes, Claude Code, Codex, or OpenCode — identically, because every containment layer wraps the *process and the pod*, not any one agent's internals. It lets an agent run genuinely unattended (schedules, event triggers, no human approving every action) because containment is enforced by the platform, not by the agent's behavior — see [`docs/design.md`](docs/design.md) for the full rationale and how this compares to a laptop CLI agent or a plain container.

![Architecture of Vicegerent Agents (Excalidraw)](./architecture.png)

## Enforcement layers

Four independent layers sit between the agent and anything it can affect. Each is enforced by a different component, so no single compromise (a bad shell command, a malicious tool result, a prompt injection) clears the whole stack — the agent has to get past all of them. Every one of these layers is enforced *around* the sandbox — by Kubernetes, Cilium, and agentgateway — not by the agent inside it.

| # | Layer | Enforces | Component | Docs |
|---|---|---|---|---|
| 1 | **Filesystem & process boundary** | What the agent process can touch inside its own pod: no root, no privilege escalation, a read-only root filesystem, no Linux capabilities, no service-account token | Kubernetes pod `securityContext` on the `Sandbox` pod (`charts/agent/templates/_sandbox.tpl`) | [AGENTS.md § Default to secure](AGENTS.md) |
| 2 | **Network egress** | Every outbound connection from the sandbox pod | Cilium (`CiliumNetworkPolicy`, kernel-level FQDN/IP allowlist) + the mitmproxy egress proxy (scrubbing, method/URL/SSRF checks) it fronts | [`charts/egress-proxy/README.md`](charts/egress-proxy/README.md) |
| 3 | **MCP tool selection** | Which tools exist for the agent to call at all | ToolHive vMCP `aggregation.tools` (`host/mcp/toolhive-servers.json`); agentgateway can also do this centrally | [`host/mcp/README.md`](host/mcp/README.md) |
| 4 | **MCP argument authorization** | What a selected tool is allowed to do with the arguments it was called with — deny, or mutate specific arguments before forwarding (e.g. keep a GitHub PR a draft, pin a Notion page's parent folder) | `mcp-cerbos-shim` + Cerbos, attached to agentgateway's `vmcp` backend (`FailClosed`) | [`images/mcp-cerbos-shim/README.md`](images/mcp-cerbos-shim/README.md) |

Layer 1 (filesystem & process boundary) is the pod itself. The `Sandbox` pod runs as an unprivileged user (uid 10000, `runAsNonRoot`), with `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all Linux capabilities dropped (`capabilities.drop: [ALL]`), a `RuntimeDefault` seccomp profile, and `automountServiceAccountToken: false` so no Kubernetes API token is even mounted. The agent can only write to a handful of explicit volumes (its data PVC at `/opt/data`, `/workspace`, `/tmp`) — everything else is immutable to it. Because this is enforced by the kubelet and the kernel against the whole container, it holds for any process the pod runs.

Layer 2 (network egress) is two things stacked, not one: Cilium enforces *which destinations exist at all* at the packet level (kernel-level — this is the layer a proxy bypass can't get around), and the mitmproxy egress proxy in front of the allowed HTTP(S) destinations enforces *what a request is allowed to look like* — GET/HEAD only, secrets scrubbed, no WebSocket upgrades, no SSRF into private ranges.

Layers 3 and 4 share one path: host vMCP → ghostunnel (mTLS) → agentgateway's `vmcp` backend → the agent. Selecting which tools exist is a config-only change in ToolHive with no cluster round-trip; authorizing a selected tool's arguments happens after that, in `mcp-cerbos-shim` + Cerbos. On allow, a mapped tool can also carry a mutation (a `force` block) — an unconditional argument rewrite applied only after Cerbos allows, e.g. a GitHub PR is rewritten to stay a draft, or a Notion page is rewritten to a fixed parent folder. It never overrides a deny. See [`AGENTS.md` § Repo Conventions](AGENTS.md#repo-conventions) ("MCP authorization layering") for the current list of denied resources and mutations.

## Repo layout

```text
charts/               Helm charts: agent, egress-proxy, platform (gateway/models/vmcp/searxng/host-firewall), cerbos-policies, mcp-cerbos-shim
stages/               staged installer manifest (stages.yaml), per-controller upstream-chart values (values/), and kubectl-applied kustomize overlays (kustomize/) incl. the one vendored tree, csi-driver-host-path (documented exception)
values.defaults.yaml  committed, fully-annotated default layer for every platform setting; layered UNDER your machine values.yaml by the installer and validate.sh
values.example.yaml   deltas-only starter; copy to a gitignored values.yaml that overrides just what differs from values.defaults.yaml (cluster vars + agents)
examples/             two complete real-world delta configs (personal.yaml, work.yaml) to model your values.yaml on
charts/*/values.yaml  intentionally empty pointer files — the platform defaults live in values.defaults.yaml
host/mcp/             host-side MCP control plane (ToolHive + vMCP) — see docs/setup.md
images/               source-built container images (hermes, agentgateway-proxy, mcp-cerbos-shim, kubernetes-mcp-server, aws-api-mcp-server, aws-profiles-mcp)
scripts/              install, secrets, validation, and test scripts driven by ./vicegerent
docs/                 design rationale (docs/design.md) and full setup walkthrough (docs/setup.md)
```

`AGENTS.md` (symlinked as `CLAUDE.md`/`HERMES.md`) is the authoritative conventions doc for anyone — human or agent — changing this repo; read it before opening an MR.

## Extending this platform

Every extension point has a working example already in the repo to copy, not just a description:

- **A new agent** — add an entry to the `agents:` list in your `values.yaml` (copy the example one); each entry becomes one `charts/agent` release. **A new model route** — copy a model template under `charts/platform/templates/models/` and toggle it in `values.yaml`; see [`AGENTS.md` § Repo Conventions](AGENTS.md#repo-conventions) ("the layout is the documentation").
- **A new machine** — a second machine is a second clone with its own gitignored `values.yaml` and its own `kind-vicegerent` cluster; see [`docs/setup.md`](docs/setup.md).
- **A new MCP server** — add an entry to `host/mcp/toolhive-servers.json` alongside the 17 already there (kubernetes, github, gitlab, jira, grafana, notion, linear, etc.); see [`host/mcp/README.md`](host/mcp/README.md) for the workload shape, tool-scoping via `aggregation.tools`, and how secrets/OAuth are wired per server.
- **A new argument-authorization rule** — add a tool mapping to `charts/mcp-cerbos-shim/files/mapping.yaml` (CEL expressions; `images/mcp-cerbos-shim/mapping.example.yaml` is a minimal worked example) and a matching Cerbos policy under `charts/cerbos-policies/policies/`; the existing `resource_github.yaml`, `resource_linear.yaml`, etc. are working deny-by-resource examples; most carry a paired `*_test.yaml` under `charts/cerbos-policies/tests/` you can copy for the new rule. See [`images/mcp-cerbos-shim/README.md`](images/mcp-cerbos-shim/README.md) for the CEL helper mechanism if the new resource needs one (e.g. normalizing a field name across spellings).
- **A mutation instead of a deny** — same mapping file, a `force` block on the tool entry (see the GitHub PR draft-forcing and Notion parent-folder-pinning entries already there for the pattern).

## Quickstart

Full walkthrough, flags, and troubleshooting: [`docs/setup.md`](docs/setup.md). Before your first install, copy `values.example.yaml` to `values.yaml` and edit it — your `values.yaml` carries only **deltas** layered over the committed `values.defaults.yaml` (the full annotated reference for every setting), and the example ships **placeholders** (`your-org/your-repo`, `you@example.com`, `PROJ`, …), not a real identity, so fill in every value before installing; `examples/personal.yaml` and `examples/work.yaml` are two complete real-world delta configs to model yours on. See [`docs/setup.md` § Values to change for your machine](docs/setup.md#values-to-change-for-your-machine). This is the condensed path on macOS with Docker:

```bash
# 1. Clone this repo
git clone <repo-ssh-url> && cd vicegerent-agents

# 2. Create the Kind cluster + Cilium CNI
./vicegerent setup cluster

# 3. Provision platform-wide secrets
export ANTHROPIC_API_KEY=***
./vicegerent setup secrets platform

# 4. Configure this machine, then provision each agent's secrets
cp values.example.yaml values.yaml
$EDITOR values.yaml          # cluster vars + the agents you want (start with the example agent)
./vicegerent setup secrets agent <name>

# 5. Install the platform (staged helm upgrade --install --wait; idempotent, re-run after any git pull)
./vicegerent install

# 6. Bring up the host-side MCP control plane (ToolHive + vMCP)
./vicegerent setup mcp       # one-time: installs thv/ghostunnel/venv, then configures MCP servers + links the CLI onto PATH
./vicegerent start

# 7. Show the agent's dashboard URL + login
./vicegerent creds <name>
```

Requires `kind`, `cilium-cli`, `kubectl`, `helm`, `yq`, `jq` on `PATH`, and an SSH key with access to your git host.

## Development

```bash
pre-commit install
pre-commit run --all-files
```

The local validation hook (`scripts/validate.sh`) expects `helm`, `yq` v4, `kubeconform`, and `python3` on `PATH` (plus `cerbos` for the policy-compile pass, skipped if absent).
