# Vicegerent Agents

Repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked agent sandboxes on a local Kind cluster (Cilium CNI), installed by a staged Helm script. The platform is **harness-agnostic**: the same sandbox runs whichever coding agent you point it at — Hermes, Claude Code, Codex, or OpenCode — identically, because every containment layer wraps the *process and the pod*, not any one agent's internals. It lets an agent run genuinely unattended (schedules, event triggers, no human approving every action) because containment is enforced by the platform, not by the agent's behavior — see [`docs/design.md`](docs/design.md) for the full rationale and how this compares to a laptop CLI agent or a plain container.

![Architecture of Vicegerent Agents (Excalidraw)](./architecture.png)

## Enforcement layers

Four independent layers sit between the agent and anything it can affect. Each is enforced by a different component, so no single compromise (a bad shell command, a malicious tool result, a prompt injection) clears the whole stack — the agent has to get past all of them. Every one of these layers is enforced *around* the sandbox — by Kubernetes, Cilium, and agentgateway — not by the agent inside it.

| # | Layer | Enforces | Component | Docs |
|---|---|---|---|---|
| 1 | **Filesystem & process boundary** | What the agent process can touch inside its own pod: no root, no privilege escalation, a read-only root filesystem, no Linux capabilities, no service-account token | Kubernetes pod `securityContext` on the `Sandbox` pod (`charts/agent/templates/_sandbox.tpl`) | [`docs/design.md`](docs/design.md) |
| 2 | **Network egress** | Every outbound connection from the sandbox pod | Cilium (`CiliumNetworkPolicy`, kernel-level FQDN/IP allowlist) + the mitmproxy egress proxy (scrubbing, method/URL/SSRF checks) it fronts | [`charts/egress-proxy/README.md`](charts/egress-proxy/README.md) |
| 3 | **MCP tool selection** | Which tools exist for the agent to call at all | ToolHive vMCP `aggregation.tools` (`host/mcp/toolhive-servers.json`); agentgateway can also do this centrally | [`host/mcp/README.md`](host/mcp/README.md) |
| 4 | **MCP argument authorization** | What a selected tool is allowed to do with the arguments it was called with — deny, or mutate specific arguments before forwarding (e.g. keep a GitHub PR a draft, pin a Notion page's parent folder) | `mcp-cerbos-shim` + Cerbos, attached to agentgateway's `vmcp` backend (`FailClosed`) | [`images/mcp-cerbos-shim/README.md`](images/mcp-cerbos-shim/README.md) |

Layer 1 (filesystem & process boundary) is the pod itself. The `Sandbox` pod runs as an unprivileged user (uid 10000, `runAsNonRoot`), with `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all Linux capabilities dropped (`capabilities.drop: [ALL]`), a `RuntimeDefault` seccomp profile, and `automountServiceAccountToken: false` so no Kubernetes API token is even mounted. The agent can only write to a handful of explicit volumes (its data PVC at `/opt/data`, `/workspace`, `/tmp`) — everything else is immutable to it. Because this is enforced by the kubelet and the kernel against the whole container, it holds for any process the pod runs.

Layer 2 (network egress) is two things stacked, not one: Cilium enforces *which destinations exist at all* at the packet level (kernel-level — this is the layer a proxy bypass can't get around), and the mitmproxy egress proxy in front of the allowed HTTP(S) destinations enforces *what a request is allowed to look like* — GET/HEAD only, secrets scrubbed, no WebSocket upgrades, no SSRF into private ranges.

Layers 3 and 4 share one path: host vMCP → ghostunnel (mTLS) → agentgateway's `vmcp` backend → the agent. Selecting which tools exist is a config-only change in ToolHive with no cluster round-trip; authorizing a selected tool's arguments happens after that, in `mcp-cerbos-shim` + Cerbos. On allow, a mapped tool can also carry a mutation (a `force` block) — an unconditional argument rewrite applied only after Cerbos allows, e.g. a GitHub PR is rewritten to stay a draft, or a Notion page is rewritten to a fixed parent folder. It never overrides a deny. See the [mcp-cerbos-shim documentation](images/mcp-cerbos-shim/README.md) for the current authorization rules and mutations.

## Choose the execution mode by supervision level

The sandbox is for autonomous or near-autonomous agents: auto mode, cron jobs, automation, and any run where a human will not inspect every command and MCP call. Its vMCP uses `aggregation.tools` to filter the available backend tools and sends calls through agentgateway and Cerbos. The optional operator vMCP is the complementary manual mode for native laptop harnesses. It still aggregates the backends and enables the `find_tool`/`call_tool` optimizer to control token usage, but deliberately omits `aggregation.tools` and bypasses agentgateway/Cerbos. This repo does not scope the operator endpoint. **If you are not willing to supervise every command, put the work in the sandbox.**

## Repo layout

```text
charts/               Helm charts: agent, egress-proxy, platform (gateway/models/vmcp/searxng/host-firewall), cerbos-policies, mcp-cerbos-shim
stages/               staged installer manifest (stages.yaml), per-controller upstream-chart values (values/), and kubectl-applied kustomize overlays (kustomize/) incl. the one vendored tree, csi-driver-host-path (documented exception)
values.defaults.yaml  committed, fully-annotated default layer for every platform setting; layered UNDER your machine values.yaml by the installer and validate.sh
values.example.yaml   deltas-only starter; copy to a gitignored values.yaml that overrides just what differs from values.defaults.yaml (policy + agents)
examples/             two complete real-world delta configs (personal.yaml, work.yaml) to model your values.yaml on
charts/*/values.yaml  intentionally empty pointer files — the platform defaults live in values.defaults.yaml
host/mcp/             host-side MCP control plane (ToolHive + vMCP) — see docs/setup.md
images/               source-built container images (agent, mcp-cerbos-shim, kubernetes-mcp-server, aws-api-mcp-server, aws-profiles-mcp)
scripts/              install, secrets, validation, and test scripts driven by ./vicegerent
docs/                 design rationale (docs/design.md), full setup walkthrough (docs/setup.md),
                      and backup/restore runbook (docs/backup-and-restore.md)
```

`AGENTS.md` (symlinked as `CLAUDE.md`/`HERMES.md`) is the authoritative conventions doc for anyone — human or agent — changing this repo; read it before opening an MR.

## Extending this platform

Every extension point has a working example already in the repo to copy, not just a description:

- **A new agent** — add an entry to the `agents:` list in your `values.yaml` (copy the example one); each entry becomes one `charts/agent` release. **A new model route** — copy a model template under `charts/platform/templates/models/`, add its values to `values.defaults.yaml`, and enable it in `values.yaml`; the existing provider templates show the required backend, route, and policy resources.
- **A new machine** — a second machine is a second clone with its own gitignored `values.yaml` and its own `kind-vicegerent` cluster; see [`docs/setup.md`](docs/setup.md).
- **A new MCP server** — add an entry to `host/mcp/toolhive-servers.json` alongside the 17 already there (kubernetes, github, gitlab, jira, grafana, notion, linear, etc.); see [`host/mcp/README.md`](host/mcp/README.md) for the workload shape, tool-scoping via `aggregation.tools`, and how secrets/OAuth are wired per server.
- **A new argument-authorization rule** — add a tool mapping to `charts/mcp-cerbos-shim/files/mapping.yaml` (CEL expressions; the ~80 entries already there are the reference, and `images/mcp-cerbos-shim/mapping.example.yaml` is a two-tool schema sketch) and a matching Cerbos policy under `charts/cerbos-policies/policies/`; the existing `resource_github.yaml`, `resource_linear.yaml`, etc. are working deny-by-resource examples; most carry a paired `*_test.yaml` under `charts/cerbos-policies/tests/` you can copy for the new rule. See [`images/mcp-cerbos-shim/README.md`](images/mcp-cerbos-shim/README.md) for the CEL helper mechanism if the new resource needs one (e.g. normalizing a field name across spellings).
- **A mutation instead of a deny** — same mapping file, a `force` block on the tool entry (see the GitHub PR draft-forcing and Notion parent-folder-pinning entries already there for the pattern).

## Quickstart

Full walkthrough, flags, and troubleshooting: [`docs/setup.md`](docs/setup.md). Before your first install, copy `values.example.yaml` to `values.yaml` and edit it — your `values.yaml` carries only **deltas** layered over the committed `values.defaults.yaml` (the full annotated reference for every setting), and the example ships **placeholders** (`your-org/your-repo`, `you@example.com`, `PROJ`, …), not a real identity, so fill in every value before installing; `examples/personal.yaml` and `examples/work.yaml` are two complete real-world delta configs to model yours on. See [`docs/setup.md` § Values to change for your machine](docs/setup.md#values-to-change-for-your-machine). Before provisioning secrets, choose the model providers and optional MCP services you will use: [`docs/setup.md` § External API keys and MCP credentials](docs/setup.md#external-api-keys-and-mcp-credentials) lists every key, what it enables, and the matching `values.yaml` switches. This is the condensed path on macOS with Docker:

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
./vicegerent setup mcp       # one-time: reconciles exact host-tool versions, creates the venv, then configures MCP servers + links the CLI onto PATH
./vicegerent start

# 7. Show the agent's dashboard URL + login
./vicegerent creds <name>

# 8. Or enter a persistent tmux session and launch any configured coding harness
./vicegerent ssh <name>

# Then choose one:
hermes
claude
codex
opencode
```

The command first opens a full-height fuzzy finder over running tmux sessions, searchable by session name and active-pane directory, so an existing repository or worktree can be resumed immediately. Even when none exist, that first menu offers a new session or a plain shell. Choosing a new session opens fuzzy repository and worktree selectors searchable by name and path. Worktree naming and prune confirmation remain inside full-height fzf screens instead of dropping to shell prompts; tmux session names are always derived from the selected repository and worktree. Special actions stay at the top of each list while the initial selection remains on the first normal item. Normal sessions, repositories, and worktrees are green; non-destructive actions are cyan; and the destructive prune action is yellow. The selectors show keyboard shortcuts for their actions: `Ctrl-N` creates a session or worktree, `Ctrl-S` opens a plain shell, `Ctrl-W` selects `/workspace`, and `Ctrl-P` opens worktree pruning. `Esc` moves back through nested selectors instead of quitting; use `Ctrl-C` to quit the selector flow. Interactive Bash uses a compact two-line prompt with the repository, branch, tracked-change marker, shortened path, and prior command failure; set `NO_COLOR=1` for its plain fallback. Pruning fuzzy-finds a linked worktree, requires confirmation, keeps the Git branch, and refuses the primary worktree or a worktree still used by a tmux pane. Detach with `Ctrl-b d` and reconnect with the same command; the coding harness keeps running if the terminal or `kubectl exec` connection drops. The tmux server still belongs to the container, so sessions do not survive an agent Pod or container restart. The same pod-level containment, credentials, egress policy, shared skills, and MCP access apply to all four harnesses. See [`docs/setup.md`](docs/setup.md) for the full shell-access walkthrough.

Requires `kind`, `cilium-cli`, `kubectl`, `helm` 4+, `yq` v4, `jq`, `git`, and OpenSSL 3 (not macOS's LibreSSL) on `PATH`, plus an SSH key with access to your git host.

## Development

```bash
pre-commit install
pre-commit run --all-files
```

The local validation hook (`scripts/validate.sh`) expects `helm`, `yq` v4, `kubeconform`, and `python3` on `PATH` (plus `cerbos` for the policy-compile pass, skipped if absent).
