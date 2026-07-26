# Setup

Full step-by-step for standing up your own instance. See the [README](../README.md) for the condensed quickstart and an overview of what you're setting up.

## Configuring for your machine

Each person runs their own clone against their own laptop and their own local Kind cluster. All machine-specific configuration lives in a single gitignored `values.yaml` (policy, agents, egress, models, and replicas) that you copy from the committed `values.example.yaml` — like copying `.env.example` to `.env`. Your `values.yaml` carries only the **deltas** for your machine; it is layered over the committed `values.defaults.yaml` (the full annotated default for every setting) by the installer and `scripts/validate.sh`, so anything you omit falls through to that default. Nothing machine-specific is committed, so a second machine is just a second clone with its own `values.yaml` (see [Adding a second machine](#adding-a-second-machine)).

### Values to change for your machine

`values.example.yaml` is **deltas-only**: it carries just the handful of values that differ from `values.defaults.yaml`, and it ships those as **placeholders** (`your-org/your-repo`, `you@example.com`, `PROJ`, `git.example.com`, …), not a real identity — the installer renders whatever you give it, so nothing fails loudly if you miss a field. Copy it and go through every field before your first install; consult `values.defaults.yaml` for the full annotated menu of settings you can also override. For two complete, filled-in references to model yours on, see `examples/personal.yaml` and `examples/work.yaml` (the maintainer's own personal and work configs, comment-free live code). The example ships Anthropic-only with content moderation and prompt-injection detection off; enable other providers/models and those gates as your machine needs them.

```bash
cp values.example.yaml values.yaml
$EDITOR values.yaml
```

Every setting carries an inline comment in `values.defaults.yaml`; the example carries only the deltas below, which you almost certainly need to change:

- **`policy`** — machine-wide authorization, data-access, content-safety, and operational limits, grouped by service. The example includes source-control identities and scopes under `policy.sourceControl`, Jira and Linear write scopes under `policy.workManagement`, the Notion Scratchpad and identity under `policy.notion`, and the Alertmanager creator under `policy.alertmanager`. Neutral defaults for data-access denylists and operational caps live in `values.defaults.yaml`.
- **`agents[].git`** — `userName` / `userEmail` are the identity each agent commits as. Ships as `your-git-username` / `you@example.com`.
- **`egress`** — `wildcardDomains` / `exactDomains` are YAML lists of external HTTP(S) destinations the egress proxy allows; `internalAllowedCIDRs` carves RFC1918 hosts out of the private-network deny.
- **`agents[].directEgress`** — `ssh.fqdn` and `ssh.cnameChain` describe your git host and its full CNAME chain; the Slack and edge-TTS lists hold direct WebSocket destinations.
- **Container registry** (`values.defaults.yaml`'s `agentDefaults.image`, `charts/mcp-cerbos-shim/templates/deployment.yaml`, `charts/platform/templates/gateway.yaml`) — these point at the original operator's Harbor registry (`harbor.hahomelabs.com/vicegerent/...`), which is public to pull from, so you can leave them as-is and install directly against it. Only repoint them if you want to build and host your own copies of `hermes-agent`, `mcp-cerbos-shim`, or the agentgateway proxy image — see each image's README under `images/*/README.md` for the build & push steps.
- **`.gitlab-ci.yml` / `renovate.json`** — these are wired for this repository's self-hosted GitLab instance. Adapt them if you host the repository elsewhere, or ignore them; they validate and update this repository but are not installed in the cluster.

### Migrating values from the former schema

The installer rejects the former flat schema instead of silently ignoring it. Move `clusterVars` into the service groups under `policy`, rename `agents[].networkAllowlist` to `agents[].directEgress`, change the SSH fields to `directEgress.ssh.fqdn` and a list-valued `directEgress.ssh.cnameChain`, change `storage.gitrepos` to `storage.gitRepos`, and convert `agents[].config` from a YAML block string to a map. Under `egress`, replace the comma-separated `apexWildcardDomains`, `exactOnlyDomains`, and `internalAllowlistCIDRs` strings with the `wildcardDomains`, `exactDomains`, and `internalAllowedCIDRs` lists. Move `egress.replicaCount` to `replicas.egressProxy`. Compare your file with `values.example.yaml` before rerunning `./vicegerent install`.

To stand up your own instance:

1. Clone this repo. `./vicegerent install` reads `values.yaml` from the checkout root.
2. Make sure the SSH key your agents will use has access to your git host, so git-over-SSH clone/push works from inside the sandbox.
3. If your git host isn't `github.com`, put its FQDN in `agents[].directEgress.ssh.fqdn` and every intermediate name in `agents[].directEgress.ssh.cnameChain` — otherwise Cilium blocks git-over-SSH from inside the sandbox. Find the chain with `dig +noall +answer <your-host>`.

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
- `openssl` 3 — the secrets scripts need `req -addext`, which macOS's stock LibreSSL lacks (`brew install openssl@3`, then put it ahead of `/usr/bin` on `PATH`)
- SSH access to your git host — see "Configuring for your machine" above

Create the cluster (creates the Kind cluster on its docker network, removes kind's auto-installed local-path-provisioner and `standard` StorageClass, installs Cilium as the CNI, and patches CoreDNS to resolve `host.docker.internal`):

```bash
./vicegerent setup cluster
```

Verify the cluster and CNI:

```bash
kubectl --context kind-vicegerent get nodes -o wide
kubectl --context kind-vicegerent get pods -n kube-system
cilium status --context kind-vicegerent
kubectl --context kind-vicegerent top nodes
```

If metrics are not ready immediately, wait a minute and rerun `kubectl --context kind-vicegerent top nodes`.

## Secrets setup

Cluster secrets are plain Kubernetes Secrets — Kind etcd is the source of truth, and no secret values live in git. The setup scripts generate crypto material (CAs, certificates, SSH keys, random tokens) and read user-supplied API keys from the environment or interactive prompts, then `kubectl apply` the Secrets directly. They are provisioned in two passes: **platform-wide** material (shared by the whole cluster) and **per-agent** material (one set per named agent). Both are idempotent — generated material already present is reused, and re-running reseeds a fresh cluster. The installer never creates secrets; it only pre-flights that the ones its workloads block on exist, and fails fast with a pointer if they don't.

MCP-server API keys are the exception: they are `thv` (ToolHive) secrets on the host, not Kubernetes Secrets. Configure them with `./vicegerent setup mcp` (see [`host/mcp`](../host/mcp)), not the scripts below.

> There is no external secret store in the loop, so **keep your own copy of every API key**. Velero backs up Kubernetes Secrets, but host-side ToolHive secrets are outside the cluster, and a backup is not a substitute for an independently held copy. Re-running the setup scripts is the supported way to seed a fresh cluster.

### Platform-wide

Generates the ghostunnel CA + server/client certificates and the egress-proxy MITM CA, generates the SearXNG signing key, the mcp-cerbos-shim self-token, and the Velero S3 credentials, and applies the model API keys you supply. The host-side ghostunnel material is written to `~/.vicegerent/ghostunnel` (override with `GHOSTUNNEL_HOST_DIR`); the CA private key never enters Kubernetes. The server cert/key + CA cert are mirrored to a `ghostunnel-server` Secret so a host missing them recovers on start. The Velero S3 credentials are likewise mirrored to `~/.vicegerent/rclone-s3/auth-key` (override with `RCLONE_S3_HOST_DIR`), which is what the host `rclone serve s3` process authenticates against.

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
<name>-ssh-key               hermes_agent_ed25519    (ed25519 private key)
```

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

Check the result:

```bash
kubectl --context kind-vicegerent get pods -A
helm --kube-context kind-vicegerent list -A
```

## Back up and restore the cluster

Velero takes a full-cluster backup daily (13:00 America/Denver, 7-day retention) — every namespaced and cluster-scoped object, the Helm release state, and the contents of the agent `data` and `gitrepos` PVCs — into an rclone S3 bucket on your laptop that survives a cluster nuke. It is the failsafe for a lost agent volume, a botched `./vicegerent install`, and an unrecoverable cluster alike.

**See [`docs/backup-and-restore.md`](backup-and-restore.md)** for the full guide: why it is built on CSI snapshots + data movement instead of a hostPath copy, which volumes are skipped, ad-hoc backups, per-agent volume restores, object-only repair restores, full-cluster restores, and the `/etc/hosts` gotcha that breaks the `velero` CLI.

## Agent volume lifecycle

The three agent PVCs (`data-<agent>`, `gitrepos-<agent>`, `models-<agent>`) are declared by the agent chart in `charts/agent/templates/pvc.yaml` and referenced from the Sandbox pod template as ordinary `persistentVolumeClaim` volumes. They are deliberately **not** Sandbox `volumeClaimTemplates`: the sandbox controller makes itself controller-owner of any claim it creates, so `kubectl delete sandbox <agent>` would garbage-collect all three claims and — since `csi-hostpath-sc` reclaims with `Delete` — take the PVs and their data with them. Chart-owned claims are unowned by the Sandbox, so deleting and recreating the Sandbox CR reattaches the same volumes. They also carry `helm.sh/resource-policy: keep`, so `helm uninstall` leaves them behind.

The second reason to own them in the chart is that labels are then re-asserted on every `helm upgrade`. A claim template's metadata is read only when the controller first creates the PVC, so a label added later never reaches an existing volume — and a Velero restore, which recreates the PVC from a backup taken before the label existed, silently drops it.

Because the data no longer depends on the Sandbox surviving, **the Sandbox CR itself is freely disposable**. It carries no `helm.sh/resource-policy: keep`, so Helm deletes and recreates it like any other object: an agent removed from `values.yaml` is `helm uninstall`ed and its Sandbox really goes away, and `kubectl delete sandbox <agent>` followed by `./vicegerent install` is a clean way to rebuild an agent from scratch. The replacement Pod remounts the same three claims; only `runtime` and `tmp` are lost, and both are `emptyDir` that the Pod rebuilds anyway.

Deleting a volume's data is therefore an explicit act:

```bash
kubectl -n agent-sandbox delete pvc models-<agent>   # then delete the Sandbox pod to have it reseed
```

## Adding a second machine

A second machine is a second clone with its own gitignored `values.yaml` and its own `kind-vicegerent` cluster. `charts/` + `stages/` are the shared, machine-agnostic platform; everything machine-specific is in `values.yaml`. On the new machine:

```bash
git clone <repo-ssh-url> && cd vicegerent-agents
cp values.example.yaml values.yaml
$EDITOR values.yaml                    # this machine's cluster vars + agents
./vicegerent setup cluster
./vicegerent setup secrets platform
./vicegerent setup secrets agent <name>
./vicegerent install
```

`scripts/install/kind-config.yaml`'s NodePort pool (`30119-30128`) only needs editing if you run two clusters on the same host at once — a single laptop running one cluster can leave it as-is.

## Host-side MCP control plane

Every MCP server runs on the laptop under ToolHive (`thv`). The control plane lives in [`host/mcp`](../host/mcp): `vicegerent mcp` brings up the 17 ToolHive workloads declared in `toolhive-servers.json` (kubernetes, github, gitlab, tavily, firecrawl, notion, linear, jira, grafana, alertmanager, pagerduty, elastic, aws — plus the `aws_profiles` companion and three regional `_gov` variants — all off by default) and supervises four long-lived host processes — the scoped `thv vmcp serve` on `127.0.0.1:4483`, `ghostunnel` (terminates cluster mTLS, listens `127.0.0.1:8453`, forwards to that vMCP), `rclone serve s3` (the Velero backup bucket on `127.0.0.1:9899`), and `mcp-health-watch` (polls workload health + AWS credential expiry and notifies) — plus opt-in `operator-vmcp` and `caffeinate` processes.

The cluster reaches the vMCP at `host.docker.internal:8453`. agentgateway fronts it with one `AgentgatewayBackend` + HTTPRoute + `AgentgatewayPolicy` trio per Gateway listener: `vmcp` on `/mcp/vmcp` for agent traffic (guardrail phase `Full`), and `vmcp-internal` on the internal `:81` listener for the shim's own re-entrant ownership lookups (phase `Request` only, so a lookup can't recurse into response inspection). Through the vMCP, tools are named `{workload}_<tool>` (e.g. `kubernetes_resources_get`).

First-time setup installs the host prerequisites (`thv`, `ghostunnel`, `supervisor`, `rclone`, `terminal-notifier`, and the Python venv `vicegerent mcp` runs under), then walks you through enabling and configuring servers interactively (API keys become `thv` secrets; notion/linear use browser OAuth), and links the `vicegerent` CLI onto your `PATH`:

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

To get a shell inside a running agent's container — it drops you into a shell in `/workspace`:

```bash
./vicegerent ssh <name>
```

VictoriaLogs (cluster-wide log aggregation) has no NodePort. Port-forward its server Service and open the web UI with:

```bash
./vicegerent logs
```

## Development

Install and run the repo hooks before committing:

```bash
pre-commit install
pre-commit run --all-files
```

The local validation hook (`scripts/validate.sh`) expects `helm`, `yq` v4, `kubeconform`, and `python3` on `PATH` (plus `cerbos` for the policy-compile pass, which is skipped if it's absent).
