# Development and extension guide

This document maps the repository and points contributors to the source of truth for common platform extensions. Read [`AGENTS.md`](../AGENTS.md) before changing the repository; it defines the required worktree, validation, image-versioning, security, and merge-request workflow.

## Repository map

| Path | Purpose |
|---|---|
| `charts/agent/` | Per-agent Sandbox, persistent volumes, harness configuration, and runtime policy |
| `charts/egress-proxy/` | Scrubbing HTTP proxy and its Cilium policy |
| `charts/platform/` | agentgateway, model routes, vMCP routes, SearXNG, and host firewall |
| `charts/cerbos-policies/` | MCP authorization policies and policy tests |
| `charts/mcp-cerbos-shim/` | Deployment and mapping for per-call MCP authorization |
| `stages/` | Ordered installation manifest, upstream chart values, and Kubernetes overlays |
| `host/mcp/` | Host-side ToolHive and vMCP control plane |
| `images/` | Source-built agent and MCP service images |
| `scripts/` | Installation, secrets, validation, migration, and test scripts |
| `docs/` | Architecture, setup, recovery, and security references |
| `examples/` | Filled machine-configuration examples for reference |
| `values.defaults.yaml` | Complete annotated platform default layer |
| `values.example.yaml` | Starter machine delta copied to the gitignored `values.yaml` |
| `vicegerent` | Main operator CLI |

The in-repository `charts/*/values.yaml` files are intentionally empty pointers. Platform defaults belong in `values.defaults.yaml`; do not restore copied defaults to individual charts.

## Common extensions

### Add an agent

Add an entry under `agents:` in the machine's gitignored `values.yaml`, then provision that agent's secrets and rerun the installer. Each entry becomes an independent `charts/agent` release with its own pod, volumes, dashboard authentication, and SSH key. See [Configuring for your machine](setup.md#configuring-for-your-machine) and [Per-agent secrets](setup.md#per-agent).

### Add or change a model route

Provider backends and routes live under `charts/platform/templates/models/`, with their configuration API in `values.defaults.yaml`. Agent-facing provider settings live under `agentDefaults.providers` and per-agent overrides.

Use the provider's real name in rendered Hermes configuration. Model pricing changes go through the repository's model-pricing patch and validator described in `AGENTS.md`; do not add ad hoc prices to values or templates.

### Add an MCP backend or tool

Host-side MCP workloads are declared in `host/mcp/toolhive-servers.json`. An entry defines how the workload starts, where its credentials live, which network destinations it needs, and which tools enter the sandbox vMCP surface. Follow the [host MCP control-plane guide](../host/mcp/README.md) for configuration and runtime behavior.

Tool selection belongs in the ToolHive vMCP aggregation. Do not approximate selection with a backend's native read-only or toolset flags when the intended boundary is whether the agent can discover and invoke a tool.

### Add an authorization rule

Add or update the tool mapping in `charts/mcp-cerbos-shim/files/mapping.yaml`, then add the corresponding Cerbos rule under `charts/cerbos-policies/policies/`. A mapping identifies the action and resource attributes; Cerbos decides whether that action is allowed.

GitHub and GitLab policy changes normally move together. Keep `resource_github.yaml` and `resource_gitlab.yaml` aligned unless the tool surfaces or workflows genuinely differ, and document a real difference in the counterpart policy header.

Existing policy tests under `charts/cerbos-policies/tests/` demonstrate resource allowlists, ownership checks, and denied actions. The [shim documentation](../images/mcp-cerbos-shim/README.md) covers normalization helpers, live lookups, content guards, and response handling.

### Force a tool argument

Use a `force` block on the mapped tool in `charts/mcp-cerbos-shim/files/mapping.yaml`. Forced values are applied only after authorization succeeds and never convert a denial into an allow. The GitHub pull-request mapping's `draft: true` rewrite is the reference example.

### Add an egress destination

Choose the path by protocol and inspection requirements:

- Ordinary external HTTP reads go through the egress proxy. Add their FQDNs to the machine's `egress` values and add a path scope when a multi-tenant host requires one.
- Traffic that cannot use the GET/HEAD-only proxy needs a dedicated, opt-in `directEgress` field and matching Cilium rule. Direct paths bypass proxy redaction and must remain as narrow as possible.
- Every Cilium hostname needs both connection and DNS permission. Exact in-cluster FQDNs depend on the Sandbox's `ndots:1` setting.

See the [egress proxy guide](../charts/egress-proxy/README.md#adding-a-new-external-service) for the complete procedure and accepted risks.

## Development environment

The repository uses one Python environment at `.venv`. `pyproject.toml` declares host-tool and validation dependencies, `uv.lock` locks the graph, and `scripts/run-python` reconciles the environment with the pinned `uv` release. The first reconciliation requires Python 3.11 or newer.

Install the repository hooks:

```bash
scripts/run-python -m pre_commit install
```

The authoritative repository render check is:

```bash
scripts/validate.sh
```

Before every commit, run the render check and pre-commit against the files changed by the branch:

```bash
scripts/run-python -m pre_commit run --files <changed files>
```

If a hook modifies a file, review the result and rerun both commands. Shell changes additionally require `bash -n` and `shellcheck`. Image build-context changes require the tag and deployed references to advance in the same merge request; run the two image-tag validation modes required by `AGENTS.md`.

The validators expect Helm 4+, `yq` v4, `jq`, and `kubeconform` on `PATH`. Cerbos policy compilation runs when the `cerbos` CLI is available.

## Delivery workflow

Use a dedicated Git worktree and branch, preserve unrelated changes, and keep one merge request focused on one concern. Run the repository-required validation, commit with the configured repository identity, push the branch, and open a draft GitLab merge request with verification results and a `Follow-up tasks` section. Do not merge the request; wait for the pipeline on the pushed commit to pass and leave it for human review.
