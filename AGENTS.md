# Vicegerent Agent Instructions

Vicegerent is a credential-isolated, egress-locked, harness-agnostic agent platform on a local Kind cluster. The repository is named `vicegerent-agents`; the project, cluster, and Kubernetes context are `vicegerent` (`kind-vicegerent`).

## Workflow

- Do not merge. Work on a dedicated branch, open a GitLab merge request, and leave it for human review.
- Preserve unrelated user changes. Before editing, confirm the worktree path and branch with `pwd` and `git branch --show-current`.
- Keep changes focused and remove obsolete configuration, comments, examples, and documentation instead of preserving them for history. Put change rationale and investigation history in the merge request, not inline.
- Use soft-wrapped Markdown: one physical line per paragraph, list item, or blockquote.
- Before every commit, run `scripts/validate.sh` and `pre-commit run --files <changed files>`. Re-run both if a hook modifies files.
- For shell changes, also run `bash -n <file>` and `shellcheck <file>`.
- Create the merge request with a complete description, verification results, and a `Follow-up tasks` section. Wait for the pipeline for the pushed commit to pass before declaring the work complete.

## Configuration and Layout

- `values.defaults.yaml` is the single annotated default layer for the platform. The gitignored `values.yaml`, committed `values.example.yaml`, and `examples/*.yaml` contain deltas only. The in-repo `charts/*/values.yaml` files are intentionally empty pointers; do not restore defaults to them.
- Omit values that repeat Kubernetes, controller-chart, application, or platform defaults. For upstream controller overrides under `stages/values/`, link to the upstream default values file rather than copying it.
- Each machine has its own clone, gitignored `values.yaml`, and `kind-vicegerent` cluster. Never commit machine-specific configuration or secrets.
- User-configurable agents, models, routes, MCP wiring, egress, and cluster variables belong in `values.yaml` and `charts/platform`. Standard controllers and their pinned versions belong in `stages/stages.yaml` and are installed from upstream charts.
- Host-side MCP servers are declared in `host/mcp/toolhive-servers.json`; the cluster-side vMCP routes are rendered by `charts/platform/templates/vmcp.yaml`. Do not move MCP servers into the cluster charts.
- In-repo charts are `charts/{platform,cerbos-policies,mcp-cerbos-shim,agent,egress-proxy}`. Do not vendor upstream controller charts. The vendored `stages/kustomize/csi-driver-host-path/` manifests are the deliberate exception.
- Keep names self-explanatory. Add a code or configuration comment only when it prevents a likely operational mistake or explains a non-obvious constraint.

## Validation

- `scripts/validate.sh` is the authoritative render check. It layers `values.defaults.yaml` under machine values and layers `agentDefaults` under each agent entry exactly as the installer does.
- When hand-rendering `platform` or `egress-proxy`, provide `--set-file secretPatterns=images/mcp-cerbos-shim/internal/server/secret-patterns.json`. Prefer `scripts/validate.sh` when possible so every chart receives the correct values slice.
- For ConfigMaps consumed by another workload, inspect the rendered ConfigMap, not only the template diff. Helm-templated files are not valid standalone YAML; validate their rendered output.
- For image changes, run both `python3 scripts/validate-image-tags.py` and `python3 scripts/validate-image-tags.py --since origin/main`. The second mode is an independent CI requirement and is not run by `scripts/validate.sh`.

## Images

- Use fully qualified image references with explicit, non-`latest` tags. Do not deploy a frozen digest or set `imagePullPolicy`/`pullPolicy: Always`; deployed images use immutable tags with `IfNotPresent`.
- Any build-context change under `images/<name>/` requires a tag bump in the same merge request, including tests and build scripts. Update the image's `Makefile` `TAG` and every deployed reference found by search.
- `hermes-agent` and `agentgateway-proxy` rebuilds retain the upstream version and increment the `-revN` suffix; reset to `-rev1` after an upstream version bump. Renovate models `revN` as a numeric build with explicit regex versioning; do not replace it with `loose`, which treats the suffix as a prerelease.
- Changes to upstream Hermes behavior belong in a numbered patch under `images/hermes/patches/`, registered in `order.txt`, with regression tests. Test patches against a disposable copy of the installed Hermes tree and verify idempotency; never mutate the running installation while validating a patch.

## Security and Policy

- Default to non-root, unprivileged containers, `automountServiceAccountToken: false` where possible, least-privilege RBAC, and fail-closed authorization. Never commit secrets. Setup scripts apply Kubernetes Secrets; ToolHive manages host MCP secrets.
- Keep GitHub and GitLab Cerbos policies in lockstep. Mirror rule changes in `charts/cerbos-policies/policies/resource_{github,gitlab}.yaml`, or document a genuine tool-surface or workflow difference in the counterpart policy's header.
- Tool selection belongs in ToolHive vMCP aggregation. Argument-level authorization and forced argument rewrites belong in the mcp-cerbos-shim mapping plus Cerbos policies. Do not use a backend's native flags to approximate tool selection. See `images/mcp-cerbos-shim/README.md` for the authorization architecture and current resource rules.
- Preserve the client-side protected-branch guard across `main`, `master`, and `production`. Changes under `images/hermes/git-guard/` must keep its tests green and the branch list aligned with the GitHub and GitLab Cerbos policies. Treat forge-side branch protection as the security boundary.
- Name the real model provider in rendered Hermes configuration; never use `custom` or `local` for a routed provider. Add prices only through `images/hermes/patches/0043-model-pricing.py`, and keep `scripts/validate-model-pricing.py` green.
- Cilium egress requires both a connection rule (`toEndpoints` or `toFQDNs`) and a DNS rule for every hostname. Prefer exact `matchName` entries, and preserve the agent Sandbox's `ndots:1` setting for exact in-cluster FQDNs.

## Storage and Secrets

- Agent `data`, `gitrepos`, and `models` PVCs use `csi-hostpath-sc`; do not make the StorageClass values-driven. Kind's `local-path-provisioner` and `standard` StorageClass are removed during cluster setup.
- Plain Kubernetes Secrets in Kind etcd are the cluster source of truth. The setup scripts generate or import them; secret values never belong in Git. MCP API keys remain ToolHive secrets on the host.
- The ghostunnel CA private key stays under `~/.vicegerent/ghostunnel` on the host. Only the required certificates and workload keys are mirrored into Kubernetes.

## Documentation Sources

- `README.md`: overview and quickstart.
- `docs/setup.md`: authoritative installation and operations walkthrough.
- `docs/design.md`: architecture rationale.
- `host/mcp/README.md`: host-side MCP inventory and operation.
- `images/mcp-cerbos-shim/README.md`: MCP authorization, live lookup, moderation, and response-guard behavior.
- `images/<name>/README.md`: image-specific build and runtime details.

Keep subsystem details beside their implementation. Add to this file only when a durable, repository-wide contributor rule cannot be enforced by code or validation.
