# Setup

Full step-by-step for standing up your own instance. See the [README](../README.md) for the condensed quickstart and an overview of what you're setting up.

## Configuring for your machine

Each person runs their own clone against their own laptop and their own local Kind cluster. All machine-specific configuration lives in a single gitignored `values.yaml` (policy, agents, egress, models, and replicas) that you copy from the committed `values.example.yaml` — like copying `.env.example` to `.env`. Your `values.yaml` carries only the **deltas** for your machine; it is layered over the committed `values.defaults.yaml` (the full annotated default for every setting) by the installer and `scripts/validate.sh`, so anything you omit falls through to that default. The installer validates the merged values as a strict configuration API before it mutates the cluster: unknown keys, wrong types, invalid release names, invalid content-safety statuses, duplicate agents, and consumers without their required model backend fail with a path-specific error. Nothing machine-specific is committed, so a second machine is just a second clone with its own `values.yaml` (see [Adding a second machine](#adding-a-second-machine)).

### Values to change for your machine

`values.example.yaml` is **mostly deltas-only** and is a practical starter for a DevOps or DevOps-adjacent Moveworks engineer. A few safety and operating defaults are repeated deliberately so the starter's intended behavior stays visible and regression-tested. It carries the shared scopes from the work profile that should work without customization: the standard Moveworks repositories and fork rule, Jira `CHANGE` project and change issue types, Linear DevOps team identifiers, maintained PagerDuty service IDs, Moveworks-wide Grafana and Elastic blocklists, Artifactory, and the `10.230.0.0/16` management-network range. The public GitHub mirror remains available. Person-specific GitHub, Jira/Linear assignee, git, Notion, and Alertmanager identities remain obvious placeholders. Slack credentials and user access are not configured, and only Anthropic is rendered; the network policy retains the exact Slack endpoints needed if Slack is configured later. Replace the identity placeholders before installing, and leave unavailable MCP backends disabled. Consult `values.defaults.yaml` for the full annotated menu of settings. [`examples/work.yaml`](../examples/work.yaml) and [`examples/personal.yaml`](../examples/personal.yaml) are the repository author's actual filled machine profiles and are kept current with the values schema. Use them when you need to see what someone really configures, but do not copy them unchanged: they contain person-specific identities, private page and service IDs, internal destinations, and provider routing.

```bash
cp values.example.yaml values.yaml
$EDITOR values.yaml
```

Every setting carries an inline comment in `values.defaults.yaml`; review these starter fields before installing:

- **`policy.sourceControl.github`** — replace `your-github-username` in the fork repositories, `forkOwners`, and `username`. The starter requires pull requests into the shared `moveworks-emu` repositories and the public platform mirror to use your fork, preventing agents from attempting direct upstream branches. Do not add a private GitLab policy block unless you actually have access and enable that MCP backend.
- **`policy.workManagement`** — Jira, Linear, and PagerDuty already contain the maintained shared DevOps scopes. Replace only the Jira/Linear assignee identity placeholders. Cerbos permits the listed project, issue types, team identifiers, and service IDs and denies targets outside those lists.
- **`policy.dataAccess`** — Grafana and Elastic use blocklists, not allowlists. The starter carries the work profile's Moveworks-wide blocks for the `fess5o6x6evb4b` / `dev-opensearch-datasource` Grafana datasource and Elastic indices matching `snowflake`. Keep those entries when enabling either permissive read-only backend, and add narrower blocks if your work requires them.
- **`policy.notion` / `policy.alertmanager`** — Notion includes the maintained DevOps, DevSecOps, DevOps WIP, and Major Incidents parent IDs; replace the Scratchpad and user placeholders after OAuth. Alertmanager permits silences up to 24 hours; replace `your-username` with the normal username used for silence ownership.
- **`agents[].git`** — replace `Your Name` / `you@moveworks.ai` with the identity each agent should commit as.
- **`egress`** — the starter includes public source/package endpoints and Moveworks Artifactory. Artifactory resolves into `10.230.0.0/16`, so that internal range must also be allowed. Any hostname that resolves to an internal IP needs its corresponding CIDR here; add only the ranges your allowed hosts require.
- **`agents[].directEgress.ssh.hosts`** — each map key is an SSH connection hostname from a git remote, and its `cnameChain` contains only the intermediate DNS aliases returned while resolving that hostname. Defaults provide no SSH hosts; every machine profile explicitly lists GitHub and any additional host it needs. Cilium rejects a CNAME answer unless each intermediate name is allowlisted. SSH bypasses the HTTP egress proxy and content scrubbing.
- **`agents[].directEgress.slackFQDNs`** — keep the four starter entries. Slack's Web API, Socket Mode WebSockets, failover WebSocket, and file downloads require direct TCP/443 because the HTTP proxy is GET-only. These network destinations do not enable Slack by themselves; Slack remains unavailable until all four required secret values are configured.
- **`agents[].config.agent.system_prompt` / `agents[].soul`** — these are intentional working defaults, not setup placeholders. Leave them intact for the standard technical-expert role and personality; edit them only when you deliberately want different agent behavior. Universal vMCP discovery guidance is rendered separately by the chart for every agent and does not belong in a machine-specific soul.
- **Container registry** (`values.defaults.yaml`'s `agentDefaults.image` and `charts/mcp-cerbos-shim/templates/deployment.yaml`) — these point at the original operator's Harbor registry (`harbor.hahomelabs.com/vicegerent/...`), which is public to pull from, so you can leave them as-is and install directly against it. Only repoint them if you want to build and host your own copies of `agent` or `mcp-cerbos-shim` — see each image's README under `images/*/README.md` for the build and push steps. The agentgateway controller and proxy use upstream images from `cr.agentgateway.dev`.
- **`.gitlab-ci.yml` / `renovate.json`** — these are wired for this repository's self-hosted GitLab instance. Adapt them if you host the repository elsewhere, or ignore them; they validate and update this repository but are not installed in the cluster.

### Migrating values from the former schema

The installer rejects the former flat schema instead of silently ignoring it. Move `clusterVars` into the service groups under `policy`, rename `agents[].networkAllowlist` to `agents[].directEgress`, move each former `directEgress.ssh.fqdn` under `directEgress.ssh.hosts.<fqdn>` with its `cnameChain`, change `storage.gitrepos` to `storage.gitRepos`, and convert `agents[].config` from a YAML block string to a map. GitHub is no longer implicitly allowed for SSH, so an agent that uses GitHub over SSH must explicitly include `github.com: {cnameChain: []}` in its hosts map. Under `egress`, replace the comma-separated `apexWildcardDomains`, `exactOnlyDomains`, and `internalAllowlistCIDRs` strings with the `wildcardDomains`, `exactDomains`, and `internalAllowedCIDRs` lists. Move `egress.replicaCount` to `replicas.egressProxy`. Also rename `agents[].tuning.gatewayTimeout` to `tuning.gatewayTimeoutSeconds`, `tuning.clarifyTimeout` to `tuning.clarifyTimeoutSeconds`, and `tuning.vmcp.timeout`/`connectTimeout` to `tuning.vmcp.timeoutSeconds`/`connectTimeoutSeconds`. DeepSeek and Z.ai platform backends now default off; a machine that enables either `agents[].providers.deepseek` or `agents[].providers.zai` must also set the matching `models.deepseek.enabled` or `models.zai.enabled` switch to `true`. Compare your file with `values.example.yaml` before rerunning `./vicegerent install`.

To stand up your own instance:

1. Clone the public mirror with `git clone git@github.com:christensenjairus/vicegerent-agents.git`. `./vicegerent install` reads `values.yaml` from the checkout root.
2. Make sure the SSH key your agents will use has access to your git host, so git-over-SSH clone/push works from inside the sandbox.
3. Each values file must explicitly list every SSH host it needs under `agents[].directEgress.ssh.hosts`. The starter lists only `github.com`, with an empty CNAME chain. To add another SSH host, use the hostname from its remote URL as a new map key, run `dig +noall +answer <your-host>`, and put only the intermediate aliases from that answer in the host's `cnameChain`. See `examples/work.yaml` or `examples/personal.yaml` for a concrete multi-host CNAME-chain example.

## Create the local Kind cluster

Prerequisites:

- macOS with Docker (Kind runs its node as a container)
- `kind`
- `cilium-cli`
- `kubectl`
- `helm` 4+ — the installer uses Helm 4 flags (`--rollback-on-failure`, `--force-replace`, `--hide-notes`) and refuses to run on Helm 3
- `yq` v4
- `jq`
- `git`
- A Homebrew-provided OpenSSL release with `req -addext` support — macOS's stock LibreSSL lacks it. Put its `bin` directory ahead of `/usr/bin` on `PATH`.
- SSH access to your git host — see "Configuring for your machine" above

Create the cluster (creates the Kind cluster on its docker network, removes kind's auto-installed local-path-provisioner and `standard` StorageClass, installs Cilium as the CNI, and patches CoreDNS to resolve `host.docker.internal`):

```bash
./vicegerent setup cluster
```

Verify the cluster and CNI:

```bash
KUBE_CONTEXT="${VICEGERENT_KUBE_CONTEXT:-kind-vicegerent}"
kubectl --context "$KUBE_CONTEXT" get nodes -o wide
kubectl --context "$KUBE_CONTEXT" get pods -n kube-system
cilium status --context "$KUBE_CONTEXT"
kubectl --context "$KUBE_CONTEXT" top nodes
```

If metrics are not ready immediately, wait a minute and rerun `kubectl --context "$KUBE_CONTEXT" top nodes`.

## Secrets setup

Cluster secrets are plain Kubernetes Secrets — Kind etcd is the source of truth, and no secret values live in git. The setup scripts generate crypto material (CAs, certificates, SSH keys, random tokens) and read user-supplied API keys from the environment or interactive prompts, then `kubectl apply` the Secrets directly. They are provisioned in two passes: **platform-wide** material (shared by the whole cluster) and **per-agent** material (one set per named agent). Both are idempotent — generated material already present is reused, and re-running reseeds a fresh cluster. The installer never creates secrets; it only pre-flights that the ones its workloads block on exist, and fails fast with a pointer if they don't.

MCP-server API keys are the exception: they are `thv` (ToolHive) secrets on the host, not Kubernetes Secrets. Configure them with `./vicegerent setup mcp` (see [`host/mcp`](../host/mcp)), not the scripts below.

> There is no external secret store in the loop, so **keep your own copy of every API key**. Velero backs up Kubernetes Secrets, but host-side ToolHive secrets are outside the cluster, and a backup is not a substitute for an independently held copy. Re-running the setup scripts is the supported way to seed a fresh cluster.

### External API keys and MCP credentials

Configure model-provider keys with `./vicegerent setup secrets platform`; set an environment variable before running it to avoid its prompt. Configure an MCP backend with `./vicegerent setup mcp`; it interactively enables only the backends you choose and writes their credentials as host-side `thv` secrets. Do not put either kind of secret in `values.yaml`.

#### Model providers

| Provider | Environment variable | Required? | What it enables | Configuration when omitted |
|---|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | **Required by the current platform-secret setup script.** The default agent and platform configuration also select Anthropic. | Claude models, Claude Code, and Anthropic-backed Hermes/Mnemosyne/MoA routes. | To use a different primary provider, update the relevant `agents[].providers`, `mnemosyne`, `failover`, `moa`, harness, and `models` values so no configured route selects Anthropic. `setup secrets platform` still currently prompts for this key even then. |
| OpenAI | `OPENAI_API_KEY` | Optional unless an enabled agent/provider route or content-safety feature uses it. | GPT/Codex/OpenCode routes and the platform's content moderation and prompt-injection detection. | Set `agents[].providers.openai.enabled: false`, disable or retarget failover/MoA/harness configuration, and set `models.openai.enabled: false` if no agent uses OpenAI. Both content-safety features are OpenAI-only today, so leave `policy.contentSafety.moderation.status` and `promptInjection.status` disabled without this key. |
| DeepSeek | `DEEPSEEK_API_KEY` | Optional. | DeepSeek's OpenAI-compatible model routes. | Both `models.deepseek.enabled` and `agents[].providers.deepseek.enabled` default to `false`; enable both when using it. |
| Z.ai / GLM | `ZAI_API_KEY` | Optional. | Z.ai/GLM standard-metered, OpenAI-compatible model routes. | Both `models.zai.enabled` and `agents[].providers.zai.enabled` default to `false`; enable both when using it. |

The `models.*.enabled` switches render platform backends; each `agents[].providers.*.enabled` switch makes that provider available to an individual agent. Both must be aligned with the keys you supplied. DeepSeek and Z.ai are opt-in at both layers. `values.example.yaml` shows an Anthropic-only configuration and explicitly disables the otherwise-enabled OpenAI platform backend.

#### MCP backends

Every MCP backend is disabled by default and optional. Enable only the services you intend an agent to use; `./vicegerent mcp doctor` checks only enabled backends. URLs, usernames, and OAuth grants in this table are credentials or connection settings rather than API keys, but are included because they are required to make the corresponding backend work.

| MCP backend | Secret or login | What it enables | Required? |
|---|---|---|---|
| GitLab | `gitlab_token` PAT (`api` scope); API URL | Read GitLab issues, labels, todos, merge requests and their discussions/drafts/diffs/approvals; create or update draft merge requests; inspect pipelines/jobs/logs and retry an existing failed job. It cannot merge, approve, write comments, or perform git-object writes. | Optional; required only when GitLab MCP is enabled. |
| GitHub | `github_token` PAT (`repo` scope) | Read issues and pull requests; create or update pull requests, request Copilot review, and update a pull-request branch. It cannot merge, approve, or write comments/reviews. | Optional; required only when GitHub MCP is enabled. |
| Tavily | `tavily_api_key` | Public-web search, extraction, crawling, mapping, and research. | Optional. Tavily and Firecrawl substantially overlap. Configure both on their free tiers so the agent can use the other when one exhausts its credits. |
| Firecrawl | `firecrawl_api_key` | Public-web scraping/search/crawling/mapping/extraction/parsing; async agents and browser interaction; website monitors; and research-paper/GitHub research tools. | Optional. It overlaps with Tavily. Configure both on their free tiers so the agent can use the other when one exhausts its credits. |
| Notion | Browser OAuth | Search/fetch pages, read comments/teams/users, and create pages, update pages, or add comments subject to the configured Cerbos parent-page rules. | Optional; no API key. |
| Linear | Browser OAuth | Read issues, projects, cycles, documents, teams, users, statuses, labels, and comments; create/update issues, projects, comments, and issue labels. Documents are read-only and deletes are not exposed. | Optional; no API key. |
| Jira | `jira_url`, `jira_username`, `jira_api_token` | Read issues, project issues, and available transitions; create/update issues, add comments, transition issues, and create issue/epic links. Deletes are not exposed. | Optional. |
| Grafana / secondary Grafana | `grafana_url`, `grafana_service_account_token` (or `_secondary` variants) | Read-only dashboards, folders, datasources, dashboard queries/properties, Prometheus metrics, Loki logs, assertions, annotations, and panel images. | Optional. |
| Alertmanager / secondary Alertmanager | API URL (or secondary URL) | Query alerts, alert groups, critical alerts, silences, receivers, status, history, and correlations; create or delete silences. | Optional; no API key in the current backend. |
| PagerDuty / secondary PagerDuty | `pagerduty_user_api_key` (or `_secondary` variant) | Read incidents, alerts, notes, schedules/on-call, services, teams, users, escalation policies, and analytics; acknowledge/resolve existing incidents and add incident notes. It cannot create incidents. | Optional. |
| Elastic | Kibana Agent Builder MCP URL and read-only `elastic_api_key` | Read streams and data quality/lifecycle information; search documents and data, run/generate ES|QL, inspect mappings/indices, and use read-only security and observability analysis. | Optional. |
| Kubernetes | Kubeconfig; optional read-only `~/.aws` mount for EKS exec auth | Read Kubernetes contexts, events, namespaces, nodes, pods/logs/top, arbitrary resources, and Helm releases. | Optional; no API key. |
| AWS / AWS profiles | Read-only host `~/.aws` mount and valid host credentials (for example SSO) | Run policy-filtered, read-only AWS CLI commands across configured profiles, receive command suggestions, and list available profile names. | Optional; no API key. |

See [`host/mcp/README.md`](../host/mcp/README.md#prerequisites) for the exact secret names and scopes. Enabling an MCP backend grants the agent its policy-filtered tools; it does not bypass the platform's tool allowlist or Cerbos authorization.

### Platform-wide

Generates the ghostunnel CA + server/client certificates and the egress-proxy MITM CA, generates the SearXNG signing key, the mcp-cerbos-shim self-token, and the Velero S3 credentials, and applies the model API keys you supply. The host-side ghostunnel material is written to `~/.vicegerent/ghostunnel` (override with `GHOSTUNNEL_HOST_DIR`); the CA private key never enters Kubernetes. The server cert/key + CA cert are mirrored to a `ghostunnel-server` Secret so a host missing them recovers on start. The Velero `velero/velero-credentials` Secret is authoritative; the host rclone auth key is reconciled from it.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # set any key to apply it non-interactively
./vicegerent setup secrets platform
```

```text
-y, --yes     auto-approve every change (non-interactive)
--force       rebuild the ghostunnel CA and all certificates from scratch
```

This applies these Kubernetes Secrets (and one ConfigMap):

```text
agentgateway-system  vicegerent-anthropic-secrets  Authorization          (Anthropic API key)
agentgateway-system  vicegerent-openai-secrets     Authorization          (optional OpenAI key)
agentgateway-system  vicegerent-deepseek-secrets   Authorization          (optional DeepSeek key)
agentgateway-system  vicegerent-zai-secrets        Authorization          (optional Z.ai/GLM key)
agentgateway-system  vicegerent-mcp-client         tls.crt, tls.key       (ghostunnel mTLS client cert)
agentgateway-system  ghostunnel-ca (ConfigMap)     ca.crt                 (ghostunnel CA cert)
agentgateway-system  ghostunnel-server             server.crt/key, ca.crt (host recovery copy)
searxng              searxng-secret                secret_key             (session/limiter signing key)
cerbos               mcp-cerbos-shim-self-token    token                  (shim's own re-entrant lookups)
egress-proxy         egress-proxy-ca               ca.crt, ca.key         (MITM CA private material)
agent-sandbox        egress-proxy-ca-cert          ca.crt                 (MITM CA cert, trust only)
velero               velero-credentials            cloud                  (rclone S3 SigV4 credentials)
```

MCP-server API keys (tavily/firecrawl/gitlab) are **not** here — they are `thv` secrets on the host (`./vicegerent setup mcp`); notion/linear use OAuth.

The ghostunnel files under `~/.vicegerent/ghostunnel`: `ca.cert`, `ca.key`, `server.crt`, `server.key`, `client.crt`, `client.key`. Only `ca.key` is host-exclusive — it stays here so a re-run can re-issue a leaf without rebuilding the chain — and the host ghostunnel server reads its material from this directory.

### Per-agent

Run once per named agent, using the name you gave it in `values.yaml`'s `agents:` list. Each agent gets its own independently generated dashboard credentials and SSH key — no material is shared between agents. Run this before `./vicegerent install`, or the install's agents-stage pre-flight will stop and point you here.

```bash
./vicegerent setup secrets agent <name>   # accepts -y/--yes
```

This applies these Kubernetes Secrets in namespace `agent-sandbox` (agent `<name>`):

```text
<name>-secrets               password, signing-secret, public-key,
                             SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
                             SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (Slack optional)
<name>-ssh-key               agent_ed25519  (ed25519 private key)
```

Slack remains optional. If any Slack value is configured, all four Slack values are required: `SLACK_ALLOWED_USERS` must be exactly one Slack user ID (`U…` or `W…`), and `SLACK_HOME_CHANNEL` must be a direct-message channel ID (`D…`). The rendered Sandbox repeats this check at startup and rejects `SLACK_ALLOW_ALL_USERS`, `GATEWAY_ALLOWED_USERS`, and `GATEWAY_ALLOW_ALL_USERS` overrides that could broaden access.

## Install the platform

`./vicegerent install` runs the staged Helm installer in `scripts/install/install.sh`. The control plane (stage order, chart coordinates, pinned versions, image tags) lives in `stages/stages.yaml`; the machine plane (`policy` / `agents` / `egress` / `models` / `replicas`) is your `values.yaml`. It does not run continuously — re-run it yourself after a `git pull` to apply upstream changes.

Each stage runs `helm upgrade --install --wait --rollback-on-failure` (or `kubectl apply -k` for the vendored/CRD manifests) in order and health-gates before moving on, so a re-run delivers upgrades with no gaps. It is idempotent — an immediate re-run with no changes is a no-op. It confirms before each change; pass `-y`/`--yes` for a non-interactive run.

```bash
./vicegerent install
```

Flags and env:

```text
-y, --yes            auto-approve every prompt (non-interactive)
    --values <file>  machine plane values (default: <repo>/values.yaml)
    --stage <name>   run only this stage
    --from <name>    run this stage and every stage after it
RECREATE=1           add `helm --force-replace` (delete/recreate on immutable-field conflict)
HELM_TIMEOUT=10m     per-release --wait timeout
VALUES_FILE=<file>   machine plane values (same as --values; the flag wins)
DEFAULTS_FILE=<file> default layer laid under it (default: <repo>/values.defaults.yaml)
```

Stages run in this order: `cni` → `crds` → `storage` → `controllers` → `platform` → `agents`. Use `--stage platform` to re-render just the platform charts after editing a cluster var, or `--from controllers` to resume partway. The `agents` stage also prunes: an agent you remove from `values.yaml` is `helm uninstall`ed on the next run (removing a controller from `stages.yaml`, by contrast, needs a manual `helm uninstall`).

The `csi-hostpath-gc` CronJob handles node housekeeping daily at 06:00 America/Denver: it prunes container images the node no longer runs, then garbage-collects orphaned csi-hostpath volume directories and snapshot files. It restores `csi-hostpathplugin` after cleanup or failure; if restoration fails, the Job prints the manual recovery command and exits nonzero. An image an upgrade replaces stays on the node until that next run, so for up to 24 hours you can roll the upgrade back by pointing the tag back and re-running `./vicegerent install`, with no re-pull or rebuild.

Check the result:

```bash
KUBE_CONTEXT="${VICEGERENT_KUBE_CONTEXT:-kind-vicegerent}"
kubectl --context "$KUBE_CONTEXT" get pods -A
helm --kube-context "$KUBE_CONTEXT" list -A
```

## Back up and restore the cluster

Velero takes a full-cluster backup daily (13:00 America/Denver, 7-day retention) — every namespaced and cluster-scoped object, the Helm release state, and the contents of the agent `data` and `gitrepos` PVCs — into an rclone S3 bucket on your laptop that survives a cluster nuke. It is the failsafe for a lost agent volume, a botched `./vicegerent install`, and an unrecoverable cluster alike.

**See [`docs/backup-and-restore.md`](backup-and-restore.md)** for the full guide: why it is built on CSI snapshots + data movement instead of a hostPath copy, which volumes are skipped, ad-hoc backups, per-agent volume restores, object-only repair restores, full-cluster restores, and the `/etc/hosts` gotcha that breaks the `velero` CLI.

## Agent volume lifecycle

The three agent PVCs (`data-<agent>`, `gitrepos-<agent>`, `models-<agent>`) are declared by the agent chart in `charts/agent/templates/pvc.yaml` and referenced from the Sandbox pod template as ordinary `persistentVolumeClaim` volumes. They are deliberately **not** Sandbox `volumeClaimTemplates`: the sandbox controller makes itself controller-owner of any claim it creates, so `kubectl delete sandbox <agent>` would garbage-collect all three claims and — since `csi-hostpath-sc` reclaims with `Delete` — take the PVs and their data with them. Chart-owned claims are unowned by the Sandbox, so deleting and recreating the Sandbox CR reattaches the same volumes. They also carry `helm.sh/resource-policy: keep`, so `helm uninstall` leaves them behind.

The second reason to own them in the chart is that labels are then re-asserted on every `helm upgrade`. A claim template's metadata is read only when the controller first creates the PVC, so a label added later never reaches an existing volume — and a Velero restore, which recreates the PVC from a backup taken before the label existed, silently drops it.

Because the data no longer depends on the Sandbox surviving, **the Sandbox CR itself is freely disposable**. It carries no `helm.sh/resource-policy: keep`, so Helm deletes and recreates it like any other object: an agent removed from `values.yaml` is `helm uninstall`ed and its Sandbox really goes away, and `kubectl delete sandbox <agent>` followed by `./vicegerent install` is a clean way to rebuild an agent from scratch. The replacement Pod remounts the same three claims; only `runtime` and `tmp` are lost, and both are `emptyDir` that the Pod rebuilds anyway.

An agent's configured name is also its Helm release name and the suffix on all three PVCs. **Renaming a deployed agent does not rename its volumes or Secrets**: changing `hermes` to `bot-jchristensen`, for example, requires new `*-bot-jchristensen` claims plus `bot-jchristensen-secrets` and `bot-jchristensen-ssh-key`; the retained `*-hermes` resources do not attach to the new release automatically. The committed `examples/*.yaml` names do not alter a machine's gitignored `values.yaml`; follow the [agent rename procedure](backup-and-restore.md#rename-an-agent-and-keep-its-volumes-and-secrets) before installing the new release or deleting any old resources.

Deleting a volume's data is therefore an explicit act:

```bash
kubectl -n agent-sandbox delete pvc models-<agent>   # then delete the Sandbox pod to have it reseed
```

## Adding a second machine

A second machine is a second clone with its own gitignored `values.yaml` and its own `kind-vicegerent` cluster. `charts/` + `stages/` are the shared, machine-agnostic platform; everything machine-specific is in `values.yaml`. On the new machine:

```bash
git clone git@github.com:christensenjairus/vicegerent-agents.git
cd vicegerent-agents
cp values.example.yaml values.yaml
$EDITOR values.yaml                    # this machine's cluster vars + agents
./vicegerent setup cluster
./vicegerent setup secrets platform
./vicegerent setup secrets agent <name>
./vicegerent install
```

`scripts/install/kind-config.yaml`'s NodePort pool (`30119-30128`) only needs editing if you run two clusters on the same host at once — a single laptop running one cluster can leave it as-is.

### Testing a second local Kind cluster

The default target remains `kind-vicegerent`. To test an existing alternate local Kind cluster without changing kubectl's active context, set `VICEGERENT_KUBE_CONTEXT` for every Vicegerent command in that test session:

```bash
export VICEGERENT_KUBE_CONTEXT=kind-test-cluster
./vicegerent setup cluster
./vicegerent install --stage cni
./vicegerent mcp start
```

The context must start with `kind-`; non-Kind contexts are rejected. The selected cluster name drives Cilium's `cluster.name`, and the Kubernetes MCP workload discovers the selected control-plane Docker network and mounts that cluster's internal kubeconfig. A user-supplied Kubernetes MCP kubeconfig continues to take precedence.

## Host-side MCP control plane

Every MCP server runs on the laptop under ToolHive (`thv`). The control plane lives in [`host/mcp`](../host/mcp): `vicegerent mcp` brings up the 17 ToolHive workloads declared in `toolhive-servers.json` (kubernetes, github, gitlab, tavily, firecrawl, notion, linear, jira, grafana, alertmanager, pagerduty, elastic, aws — plus the `aws_profiles` companion and three `_secondary` variants — all off by default) and supervises four long-lived host processes — the scoped `thv vmcp serve` on `127.0.0.1:4483`, `ghostunnel` (terminates cluster mTLS, listens `127.0.0.1:8453`, forwards to that vMCP), `rclone serve s3` (the Velero backup bucket on `127.0.0.1:9899`), and `mcp-health-watch` (polls workload health + AWS credential expiry and notifies) — plus opt-in `operator-vmcp` and `caffeinate` processes.

The cluster reaches the vMCP at `host.docker.internal:8453`. agentgateway fronts it with one `AgentgatewayBackend` + HTTPRoute + `AgentgatewayPolicy` trio per Gateway listener: `vmcp` on `/mcp/vmcp` for agent traffic (guardrail phase `Full`), and `vmcp-internal` on the internal `:81` listener for the shim's own re-entrant ownership lookups (phase `Request` only, so a lookup can't recurse into response inspection). Through the vMCP, tools are named `{workload}_<tool>` (e.g. `kubernetes_resources_get`).

First-time setup reconciles the host prerequisites (`thv`, `ghostunnel`, `supervisor`, and `rclone`) to the exact versions in [`host/brew/packages.json`](../host/brew/packages.json), builds and authorizes the native Vicegerent notification helper, reconciles the repository's locked root `.venv`, then walks you through enabling and configuring servers interactively (API keys become `thv` secrets; notion/linear use browser OAuth), and links the `vicegerent` CLI onto your `PATH`. Repository-managed versioned formulae live under `Formula/`; notification-helper source lives under `host/notifier`. `./vicegerent host-packages check` is the read-only drift check and `apply` repairs drift explicitly. Every `vicegerent start` and `vicegerent mcp start` runs the independent drift probes concurrently and automatically applies drift with `--yes` before the host stack starts:

When macOS first prompts, allow Vicegerent notifications, then select **Persistent** rather than **Temporary** under **System Settings → Notifications → Vicegerent → Alert Style**. Re-run host-package apply after changing the style. The persistent style is part of the managed health contract, so check/apply reports drift until it is selected.

```bash
./vicegerent setup mcp
```

Re-run it any time to reconfigure, or toggle an individual server later with `./vicegerent mcp enable <key>` / `disable <key>`.

Start and stop the whole local platform — the Kind cluster and the host MCP stack together — with the top-level commands:

```bash
./vicegerent start   # start the Kind cluster, then bring up the host MCP stack
./vicegerent stop    # stop the host MCP stack (including ToolHive workloads), then stop the cluster
```

For finer control of just the host stack, drive it with `./vicegerent mcp` (`configure`, `enable`/`disable`, `start [--caffeinate] [--operator-vmcp]`, `stop`, `status`, `logs`, `doctor`); the interactive TUI is the top-level `./vicegerent tui`. `--operator-vmcp` adds an unscoped, optimized endpoint at `http://127.0.0.1:4484/mcp` for native harnesses in manual mode. Both vMCPs aggregate the same ToolHive backends and enable the `find_tool`/`call_tool` optimizer, but only the sandbox vMCP uses the `aggregation.tools` filter. The operator endpoint deliberately bypasses that filter and agentgateway/Cerbos; this repo attempts no operator-side tool selection or argument authorization. Use it only while actively supervising every action. If you are not willing to supervise every command, run the work in the sandbox. A later `start` without the flag removes the operator process. There is one more subcommand, `mcp-health-watch` — supervisord runs it as the fourth always-on supervised process, and you should not invoke it by hand. See [`host/mcp/README.md`](../host/mcp/README.md) for the full reference and host-trust warning.

```bash
./vicegerent mcp start
# Or, while using a trusted native harness under manual supervision:
./vicegerent mcp start --operator-vmcp
./vicegerent mcp status
```

## Dashboards

Each agent's Hermes dashboard is published on a Kind NodePort — derived as 30119 plus the agent's index in your `agents:` list (pool `30119-30128`, mapped to the host via kind `extraPortMappings`) — and reachable directly at `http://127.0.0.1:<nodePort>/`. Print its URL + basic-auth credentials, then open the URL in a browser:

```bash
./vicegerent creds <name>   # print dashboard URL + login
```

To open a persistent tmux session inside a running agent's container:

```bash
./vicegerent ssh <name>
```

The command always opens a fuzzy finder over each session's name, active pane directory, window count, attachment state, and creation time. Select one to resume it immediately without navigating the repository and worktree selectors, or use `Ctrl-N` for a new session or `Ctrl-S` for a plain shell. With no running sessions, the same menu offers only those two actions.

Directory selection uses full-height fuzzy finders for the Git repositories immediately under `/workspace` and for each selected repository's registered worktrees. Both selectors are searchable by name and path. Worktree names are entered through a full-height fzf query screen, and prune confirmation is another fzf selection, so the flow never drops to a shell prompt during normal navigation. Invalid worktree names and Git failures exit the selector and display their diagnostics in the terminal. Special actions stay at the top of each list while the initial selection remains on the first normal item, so the actions remain visible without making them the default. Normal sessions, repositories, and worktrees are green; non-destructive actions are cyan; and the destructive prune action is yellow. `Ctrl-W` selects `/workspace` from either directory selector, `Ctrl-N` creates a detached worktree at `<repo>/.worktrees/<name>` without creating or reserving a branch, and `Ctrl-P` opens the fuzzy prune-target selector. If a requested new session's derived name already exists, a confirmation resumes it by default or returns to that repository's worktree selector. `Esc` returns from prune confirmation to worktrees, from prune targets to worktrees, and from worktrees to repositories; it returns from repositories to sessions. At the top-level selector, `Esc` stays in the selector; use `Ctrl-C` to quit the flow. Worktree names can contain `/` to create matching subdirectories. Pruning keeps any checked-out branch, excludes the primary worktree from the selector, and refuses any path used by a tmux pane. A normal prune preserves Git's dirty-worktree protection; when Git refuses a dirty worktree, a separate red confirmation can force removal and permanently delete its modified and untracked files. New tmux sessions always use a name derived from the selected repository and worktree.

Interactive Bash has a compact two-line prompt: the first line shows the user and Pod, repository and branch, a `*` for tracked Git changes, and a shortened working path; the second line shows a red `✗ <status>` after a failed command before the cyan `❯` input marker. It sets the terminal title to the repository/branch and path when the terminal supports titles. The prompt works in tmux, avoids Git work outside a repository, and caches its Git state for up to one second. `NO_COLOR=1` or `TERM=dumb` uses a plain `>` prompt instead.

Detach with `Ctrl-b d`; closing the terminal or losing the `kubectl exec` connection leaves the session and its foreground process running. Tmux session names are derived from the selected repository and worktree, so use the top-level selector to resume an existing session or create one. Use `--list` to inspect sessions without attaching, or `--shell` to bypass tmux and repository selection.

The tmux server runs inside the agent container. It survives only connection loss, not a Pod or container restart; the persistent `data` and `gitrepos` volumes preserve harness state and workspace files, but cannot preserve running processes.

The installed image includes all four configured coding harnesses. From the session, start whichever one fits the task; the same pod-level containment, credentials, egress policy, shared skills, and MCP access apply to each:

```bash
hermes
claude
codex
opencode
```

VictoriaLogs (cluster-wide log aggregation) has no NodePort. Port-forward its server Service and open the web UI with:

```bash
./vicegerent logs
```

## Development

Install and run the repo hooks before committing:

```bash
scripts/run-python -m pre_commit install
scripts/run-python -m pre_commit run --all-files
```

The repository has one Python environment at `.venv`. `pyproject.toml` declares every host-tool and validation dependency, `uv.lock` locks the full cross-platform graph, and `scripts/run-python`, setup, and validation reconcile it with `uv sync --locked`. The first reconciliation needs Python 3.11+ and bootstraps the pinned `uv` inside the venv without modifying Homebrew's externally managed Python. Host MCP runtime commands use the environment created by `./vicegerent setup mcp` without performing package installation, so teardown remains available during a package-source outage. The local validation hook (`scripts/validate.sh`) also expects `helm`, `yq` v4 (the Go binary from [mikefarah/yq](https://github.com/mikefarah/yq), not the same-named PyPI `yq`), and `kubeconform` on `PATH` (plus `cerbos` for the policy-compile pass, which is skipped if it's absent).
