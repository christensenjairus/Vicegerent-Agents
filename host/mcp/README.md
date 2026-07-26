# Host stack control plane

This directory owns the host-side ToolHive stack that backs the cluster's MCP access. `vicegerent mcp` brings up ToolHive workloads, aggregates them behind a single vMCP endpoint, and exposes that to the cluster over mTLS.

Stack shape:

```text
agent sandbox
  -> agentgateway
  -> ghostunnel (mTLS, listen 127.0.0.1:8453, reached via host.docker.internal:8453)
  -> ToolHive vMCP (loopback 127.0.0.1:4483, prefixes each backend's tools with {workload}_)
  -> 17 ToolHive workloads (group 'vicegerent')
```

`thv` runs the workloads as Docker containers under ToolHive's own daemon — they persist across `start`/`stop` so OAuth tokens are not re-prompted. `start` detects when a workload's declared spec (package, env, run/server flags, secret targets) has drifted from what's actually running — e.g. editing `env` or `tools` for a server already up — and recreates that one container instead of `thv restart`-ing it (which would silently keep the OLD args forever, since restart reuses whatever was passed to the container's original `thv run`). Supervisord manages the long-lived host processes:

- `vmcp` — `thv vmcp serve` aggregating the group on 127.0.0.1:4483.
- `ghostunnel` — terminates mTLS from the cluster (client CN `agent-client`) and forwards to vMCP.
- `rclone-s3` — `rclone serve s3` on 127.0.0.1:9899, the S3 backend for the cluster's Velero backups.
- `mcp-health-watch` — always on, no flag. Polls every *enabled* workload's own `thv list` status and fires a macOS notification the first time one drops out of "running" (e.g. an OAuth-backed remote like Notion or Linear losing its token and going `unauthenticated`/`error` — observed live: the workload drops out of vMCP entirely until `start` brings it back), and — whenever the `aws` server is enabled — additionally warns *before* that backend's AWS credentials expire (and again once expired). See "MCP health & credential watcher" below.
- `caffeinate` — opt-in; keeps macOS awake for as long as the stack is up.

## The 17 backends (group `vicegerent`)

The workload name is the vMCP tool prefix, and the Cerbos policy keys off it — these names are load-bearing, defined in `toolhive-servers.json`:

| Workload | Run | Auth |
|---|---|---|
| `kubernetes` | custom `harbor.hahomelabs.com/vicegerent/kubernetes-mcp-server` (adds AWS CLI to upstream's `kubernetes-mcp-server`, `--read-only --toolsets=config,core,helm`) | kind `--internal` kubeconfig or a user kubeconfig, plus a mandatory read-only `~/.aws` mount (blank = `~/.aws`) for EKS exec auth |
| `gitlab` | npx `@zereight/mcp-gitlab` | `gitlab_token` secret |
| `github` | registry `ghcr.io/github/github-mcp-server:v1.5.0` (container — deliberately held below v1.6.0, see "Version pinning" below) | `github_token` secret |
| `tavily` | npx `tavily-mcp` | `tavily_api_key` secret |
| `firecrawl` | npx `firecrawl-mcp` | `firecrawl_api_key` secret |
| `notion` | registry remote `notion-remote` | OAuth (browser, first run) |
| `linear` | registry remote `linear` | OAuth (browser, first run) |
| `jira` | registry `ghcr.io/sooperset/mcp-atlassian:0.22.1` (container) | `jira_url` + `jira_username` + `jira_api_token` secrets |
| `grafana` | registry `docker.io/grafana/mcp-grafana:0.17.2` (container, `--disable-write --disable-proxied`) | `grafana_url` + `grafana_service_account_token` secrets |
| `alertmanager` | npx `mcp-alertmanager` | `url` param (`--url`), no secret |
| `pagerduty` | registry `ghcr.io/stacklok/dockyard/uvx/pagerduty-mcp:1.1.0` (container) | `pagerduty_user_api_key` secret |
| `elastic` | remote transport to Kibana Agent Builder (URL param) | `elastic_kibana_url` + `elastic_api_key` secrets |
| `aws` | custom `harbor.hahomelabs.com/vicegerent/aws-api-mcp-server` (non-blocking patch of upstream awslabs image, read-only) | read-only `~/.aws` mount (SSO), multi-profile |
| `aws_profiles` | custom `harbor.hahomelabs.com/vicegerent/aws-profiles-mcp` (hidden companion of `aws`) | read-only `~/.aws` mount |

Three `_gov` variants — `grafana_gov`, `alertmanager_gov`, `pagerduty_gov` — are GovCloud/gov-endpoint twins of `grafana`/`alertmanager`/`pagerduty` (same run/auth, different host param) and complete the count of 17.

Tool scoping uses the vMCP's native `aggregation.tools` primitive: a server with a `tools` allowlist in `toolhive-servers.json` emits a `{workload, filter}` entry so the vMCP exposes only those tools (raw, unprefixed names). This is deliberately the ONLY place tool selection narrows anything — a backend's own native flags/env vars are reserved for forcing read-only/non-destructive mode (`kubernetes`'s `--read-only`, `grafana`/`grafana_gov`'s `--disable-write`) or for *expanding* its own tool surface so more is available to allowlist from (`pagerduty`/`pagerduty_gov`'s `--enable-write-tools`, `gitlab`'s `USE_PIPELINE`, `github`'s `GITHUB_TOOLSETS=all`, `jira`'s `TOOLSETS=all`). Narrowing at the source also hides tools from a raw probe (`scripts/probe-mcp-tools.py` talks to each backend's own endpoint directly, ahead of any vMCP filtering) even though they're already uncallable via the allowlist below — costing visibility into what a backend *could* offer with no corresponding safety benefit. Two flags are exceptions:

- `grafana`/`grafana_gov`'s `--disable-proxied` drops *proxied* tools — ones mcp-grafana re-exports from MCP servers embedded in the target Grafana's own datasources (Tempo, etc.). That set is discovered at startup from whatever the remote instance happens to have enabled, so it can't be pinned in a static allowlist or a committed probe at all; refusing it at the source is the only stable option.
- `kubernetes`'s `--toolsets=config,core,helm` is a genuine violation, not an exception: it narrows at the source and is why `docs/available-mcp-tools/kubernetes.yaml` lists 16 tools instead of the server's full surface. The equivalent scoping belongs in this backend's `tools` allowlist; moving it needs a re-probe without the flag first, so the inventory reflects what the image can actually do. Every backend now carries a `tools` allowlist — for `tavily`/`firecrawl` it's the full live tool set pinned explicitly (nothing to restrict — tavily/firecrawl have no write capability against anything this platform owns; pinning just stops a future package bump from silently adding to what's exposed), for `alertmanager` it's the full 12-tool set including `createSilence`/`deleteSilence` (an explicit choice, not an oversight — the operator wants the agent able to manage silences), and for `elastic` it's the 24 read/analysis tools (the 3 write tools are excluded). The rest genuinely restrict:

- `kubernetes` — read-only at the source (`--read-only` makes writes impossible regardless of allowlist), and the allowlist drops 2 of the 16 tools `--toolsets=config,core,helm` leaves. `configuration_view` is the security-relevant one: despite being `readOnlyHint=true`, it returns the full kubeconfig including `client-certificate-data`/`client-key-data` in plaintext — a live cluster credential handed straight into agent context and transcripts. `configuration_contexts_list` (names + server URLs only, no key material) covers "what clusters/contexts exist" instead. `projects_list` is dropped as noise: it lists OpenShift `Project` objects, which neither the kind cluster nor EKS has.
- `pagerduty` — incidents R/W + read-only schedules (v2 and v3)/services/teams/users/escalation policies/analytics metrics.
- `grafana` — read-only search/datasource/dashboard/prometheus/asserts/annotations/rendering.
- `jira` — read+write only, Confluence disabled (no Confluence creds — mcp-atlassian gates that by service-credential availability regardless of any toolset setting), deletes excluded, confined to the CHANGE project via `JIRA_PROJECTS_FILTER`.
- `github` — the PR lifecycle short of merging or leaving any comment/review text; no issue tools (this operator doesn't use GitHub issues at work) and no generic git file/branch-write tools (SSH access to github.com covers routine git instead).
- `gitlab` — issues + the MR lifecycle short of merging, approving, or commenting; read-only pipeline status/logs plus retrying an existing failed/canceled job (`retry_pipeline_job`). 39 of the pinned build's 134 tools (38 project-scoped ones plus `get_project`, which the Cerbos shim uses to canonicalize any spelling of a project id to its numeric id before matching the allowlist). What the exclusions actually buy, in rough order of how much it matters: every git-object write (`push_files`, `create_or_update_file`, `create_branch`, `delete_branch`, `protect_branch`, `unprotect_branch`, `update_default_branch`) — the bot has SSH access to the instance, so git itself does this; **every note/thread/discussion/draft-note WRITE tool** (`create_note`, `create`/`update`/`delete_merge_request_note`, the three `_discussion_note` variants, `create_merge_request_thread`, `resolve_merge_request_thread`, `create`/`update`/`delete`/`publish_draft_note`, `bulk_publish_draft_notes`, `create`/`update_issue_note`) — the operator does not want the bot leaving any comment or review text under their identity, the same call made on the GitHub side, and the edit/delete variants were the sharper half since `note_id` is an opaque integer a hallucinated id could use to silently rewrite or delete a human's comment; namespace-level creation (`create_repository`, `create_group`, `fork_repository`, `update_project`); the two decisions a human owns (`merge_merge_request`, `approve_merge_request`/`unapprove_merge_request`); starting or aborting pipeline work rather than retrying a failed job (`create_pipeline`, `retry_pipeline`, `cancel_pipeline`, `cancel_pipeline_job`, `play_pipeline_job`); the deletes (`delete_issue`, `delete_issue_link`, `delete_label`); and repo-content reads that git over SSH already covers (`get_file_contents`, `get_repository_tree`, `list_commits`, `search_repositories`). The note/discussion **read** tools (`mr_discussions`, `get_merge_request_note`/`notes`, `get_draft_note`, `list_draft_notes`) stay — reading review threads is the point. The 12 emoji-reaction tools are dropped as pure noise. There is no GraphQL, wiki, release, tag, milestone, webhook, or CI/CD-variable tool to exclude — `@zereight/mcp-gitlab@2.1.42` gates those behind env toggles this config does not set, so they never reach the vMCP.
- `linear` — Linear's real surface has grown to 52 tools, including 4 destructive deletes and several newer feature categories (attachments, releases, milestones, status updates, and an 8-tool diff/code-review category — get_diff/list_diffs/get_diff_threads/save_diff_comment/resolve_diff_thread/delete_diff_comment/submit_diff_review/merge_diff); this allowlist keeps 21 tools — the original functional scope of issues/comments/projects/labels/statuses/teams/users, read+write, no deletes, via the renamed save_issue/save_comment/save_project — and excludes the rest pending a deliberate follow-up. Documents are **read-only** here: `get_document`/`list_documents`/`search_documentation` are allowlisted, `save_document` is not. The Cerbos guardrail below confines a write's team to `${linearAllowedTeams}` instead.
- `notion` — 5 read tools (notion-search, notion-fetch, notion-get-comments, notion-get-teams, notion-get-users) plus notion-create-pages/notion-update-page/notion-create-comment.
- `elastic` — the 24 read/analysis tools (streams, core search/ES|QL/index, security, observability); the 3 write tools (create-visualization, create-detection-rule, resume-workflow-execution) are excluded. The Cerbos guardrail below additionally denies any data-access call targeting a blocked index/datastream token.

Doing tool selection here (rather than as a per-tool allowlist in agentgateway, which it also supports) keeps it a quick host-side edit for developers; a centralized corporate deployment would more likely enforce that allowlist at the gateway.

### Network egress lockdown

Every backend also carries a `network` block in `toolhive-servers.json`, enforced via ToolHive's native `--permission-profile` mechanism (network isolation is ToolHive's default since v0.30.1 — no `--isolate-network` flag needed to turn it on). `build_permission_profile()`/`write_permission_profile()` in `vicegerent_mcp.py` turn that config into a per-server JSON profile (`network.outbound.allow_host`/`allow_port`) written to the runtime dir and passed as `--permission-profile <path>` at `thv run` time, so each container's egress is locked to exactly the hosts it needs — anything else is denied by ToolHive's own egress proxy, independent of and in addition to the Cerbos/tool allowlisting above.

`network` takes one of these shapes:

- **`allow_hosts: [...]`** — static hostnames, safe to hardcode because they don't vary across users/clusters (fixed cloud endpoints): `github` (github.com, api.github.com), `notion` (mcp.notion.com — the official hosted remote), `linear` (mcp.linear.app — the official hosted remote), `tavily` (api.tavily.com), `firecrawl` (api.firecrawl.dev), `pagerduty`/`pagerduty_gov` (api.pagerduty.com — the PagerDuty MCP server's own docs confirm this is the only host used unless `PAGERDUTY_API_HOST` is overridden for an EU account, which this config doesn't do for either workload), and `aws` (`.amazonaws.com`, `.api.aws`). The leading dot is a suffix match — ToolHive's egress proxy is squid and turns each `allow_hosts` entry into an `acl … dstdomain <host>`, so a leading-dot entry covers every subdomain: one `.amazonaws.com` allows every `<service>.<region>.amazonaws.com` (all regions incl. gov, plus the SSO oidc/portal and read-only-classification endpoints) without enumerating them, and `.api.aws` covers newer service endpoints + the `suggest_aws_commands` endpoint. Verified via the egress proxy's access log.
- **`host_from_param: "<param name>"`** — the hostname is parsed (via `urllib.parse.urlparse`) out of a `params[]` entry's *resolved* value at `thv run` time — never hardcoded, since it's per-operator/per-cluster. Covers `gitlab` (its `api_url` param) and `alertmanager`/`alertmanager_gov` (their `url` param), and `elastic` (its `kibana_url` param). Raises a clear error (same pattern as the existing kubeconfig check) if `./vicegerent setup mcp` hasn't set that param yet.
- **`host_from_secret: "<thv secret name>"`** — same idea, but for a hostname that lives in a top-level `secrets[]` entry instead of `params[]` (fetched directly via `thv secret get`). Covers `jira` (`jira_url`) and `grafana`/`grafana_gov` (`grafana_url`/`grafana_gov_url`).
- **`exempt: true`** — out of scope for permission-profile allowlisting entirely. Only `kubernetes`: it already opts out of ToolHive's network isolation via `--isolate-network=false` (see "Kubernetes networking" below) because it needs raw docker-network TCP to the kind API server, which the egress proxy (HTTP/HTTPS only) can't front. Because this is an unconditional opt-out (not a narrower allowlist), it's *also* what makes pointing `kubeconfig` at a real AWS EKS cluster reachable with no extra config — there's no separate `allow_hosts` entry to add for the EKS API endpoint; don't mistake `exempt` for still being squid-fronted.
- **`none: true`** — deny-all egress (a permission profile with an empty allow-list). Only `aws_profiles`: it makes no outbound calls, just reads the mounted `~/.aws/config` and serves stdio through ToolHive's bridge.

A change to `network` (a new allowlisted host, an edited hostname param) is folded into `server_spec_fingerprint()`'s drift-detection hash, so `start` recreates the affected workload instead of leaving a stale `--permission-profile` baked into an already-running container. The same fingerprint also folds in the CONTENT of a server's mounted host config (the `aws`/`aws_profiles` `~/.aws` directory, a user-supplied kubeconfig): an `aws sso login`, a newly added profile, or an edited kubeconfig changes the hash, so the next `start` recreates the workload with the fresh config rather than relying on the live bind mount (some servers read their config only once at startup). The kind-cluster internal kubeconfig's CA rotation is handled separately by `kind_kubeconfig_stale`.

### Version pinning

Every server's `registry`/`package` value must resolve to an exact, reproducible artifact — a full `image:tag` for `type: registry`, or `name@version` for `type: npx` — never a bare ToolHive registry name (`grafana`, `io.github.stacklok/github`, etc.). A bare name floats on whatever ToolHive's own registry currently resolves it to, which can change silently between two machines or two points in time with zero diff in this repo to show for it — this bit us once already (a Grafana MCP behavior change traced back to an unpinned `registry: "grafana"` resolving to a different upstream version than expected). `kubernetes`, `aws`, and `aws_profiles` show the pattern for a custom/harbor image (`harbor.hahomelabs.com/vicegerent/<name>:vX.Y.Z`); `gitlab`/`tavily`/`firecrawl`/`alertmanager`/`alertmanager_gov` show it for `npx` packages; `github`, `jira`, and `pagerduty`/`pagerduty_gov` were pinned the same way, with a fresh `docs/available-mcp-tools/<name>.yaml` probe against the pinned version as the audit trail. `grafana`/`grafana_gov` were pinned to `docker.io/grafana/mcp-grafana:0.17.2` too, but WITHOUT that same re-probe (`docs/available-mcp-tools/grafana.yaml` still reflects an unrelated, earlier probe from 2026-07-04, before this pin) — treat its `mapping.yaml` `grafana_datasource` argument-name assumptions as unverified against 0.17.2 until someone does that probe. Bump a pin manually when you deliberately want a newer version — and re-diff `docs/available-mcp-tools/<name>.yaml` (`scripts/probe-mcp-tools.py`) against the old one afterward, since a version bump can change or drop tools the shim's Cerbos mapping (`charts/mcp-cerbos-shim/files/mapping.yaml`) or the `tools` allowlist here assumes still exist. `github` is a second incident on top of that first one, and a different failure mode from "the wrong version got resolved": it's pinned to `v1.5.0` even though `v1.6.0` is the latest release, because `v1.6.0` rejects a second `initialize` on the same stdio connection as a hard protocol error, and ToolHive's vMCP sends one per incoming client session/health-check over the single shared stdio pipe — so `v1.6.0` never successfully aggregates through the vMCP at all (confirmed live: a fresh `v1.6.0` container's SECOND initialize, ever, already fails; a fresh `v1.5.0` container tolerates 15 in a row). Bumping this specific pin needs that same live A/B check repeated against the target version first, not just a tool-catalog re-diff — a version can be a strict superset of tools/args and still be unusable through the vMCP.

`aws_profiles` is a **hidden companion** of `aws` (`companion_of: aws`): it's enabled/disabled with `aws` as one unit, never shown or configured on its own (so a developer needn't know it exists), and inherits `aws`'s `~/.aws` mount config. Its sole tool (`aws_profiles_list`) lets the agent discover which `--profile` values `call_aws` accepts — the `aws` backend can't enumerate profiles itself.

Orthogonal argument-level authorization still lives in the cluster (the Cerbos guardrail on the `vmcp` backend); no Cedar/authz runs in the vMCP.

Fourteen Cerbos policies cover kubernetes, grafana, elastic, aws, jira, github, gitlab, linear, alertmanager (silences and alert queries), pagerduty, notion, and firecrawl/tavily crawling. `images/mcp-cerbos-shim/README.md` ("Authorization Layers") has the per-resource deny table; don't duplicate it here. Two things about that split are specific to this file:

- **GitHub and GitLab are held to the same standard**, deliberately: `resource_gitlab.yaml` mirrors `resource_github.yaml` rule-for-rule (project/repo allowlist on every mapped tool including reads, protected-branch block, reviewer/assignee deny, live-resolved own-PR/own-MR gate, `force: {draft: true}` on create+update), and any rule added to one must be ported to the other or its absence justified in the receiving policy's header. GitLab is a self-hosted instance the operator owns, which is a reason to get the policy right, not a reason to skip it.
- **Notion**'s `create-pages` is *denied* on any parent other than the Scratchpad page, with a message naming the correct `page_id`. It used to be force-rewritten to Scratchpad instead; that silently discarded whatever parent the agent intended and taught it nothing, so it kept guessing on every future call. The only surviving `force` blocks are GitHub's `draft: true` on `create_pull_request`/`update_pull_request`, GitLab's `draft: true` on `create_merge_request`/`update_merge_request`, and Alertmanager's `createdBy` stamp on `createSilence`.

Linear's `save_issue` schema (`team`, not `teamId`) was confirmed against a live `tools/list` call. GitLab's `get_merge_request` shape (a single MR object carrying a nested `author.username`, for both the `merge_request_iid` and `source_branch` selectors) was confirmed the same way, against the live instance.

### Tool discovery optimizer

With 17 backends aggregated, the raw tool count is large enough to burn a meaningful chunk of the agent's context budget just listing tool definitions. `thv vmcp serve --optimizer` (Tier 1, FTS5 keyword search, no extra container) collapses the exposed surface to two meta-tools — `find_tool` (search) and `call_tool` (invoke by name) — so the agent discovers tools on demand instead of loading all of them up front. It's on by default (`generate_vmcp_config`'s caller passes `--optimizer`); set `VMCP_OPTIMIZER=0` before `./vicegerent mcp start` to fall back to exposing every tool raw.

This requires `mcp-cerbos-shim` to unwrap `call_tool`'s wrapped `{tool_name, parameters}` back into the real tool name before its mapping/Cerbos lookup (see `images/mcp-cerbos-shim/README.md` "How it works") — without that, every optimizer-routed call looks identical to the shim and the Secret/OpenSearch/ Jira-project guardrails would silently stop applying. Don't enable `--optimizer` against an older shim image that predates this unwrap.

### Kubernetes networking (the gotcha)

`thv` containerizes the k8s server, so it lives in the Docker VM and cannot reach a host-loopback API. The container is attached to the kind docker network (`--network vicegerent --isolate-network=false`) and, with a blank `kubeconfig` param, fed kind's `--internal` kubeconfig (`kind get kubeconfig --name vicegerent --internal`, server `https://vicegerent-control-plane:6443`), mounted read-only and pointed at via `KUBECONFIG` + `--kubeconfig`.

`kubernetes` runs a custom image (`images/kubernetes-mcp-server`, not ToolHive's generic `npx://` protocol) that bundles the AWS CLI alongside the `kubernetes-mcp-server` npm package. That's needed to point `kubeconfig` at a *real* cluster instead: a kubeconfig from `aws eks update-kubeconfig` carries an `exec:` IAM-authenticator plugin that shells out to `aws eks get-token` at request time, using the operator's ambient AWS credentials — and that exec call runs inside this same container, so it needs the `aws` binary on `PATH` and the operator's AWS config available. The `aws_config_dir` param (`apply: "aws_config"`, same as the `aws` backend) always mounts `~/.aws` read-only for exactly that — blank defaults to `~/.aws`, and `start` fails if that directory doesn't exist, even for a kind-cluster-only setup (SSO refresh stays host-side, same as the `aws` backend). The agent's tool surface doesn't change either way — `aws eks get-token` runs as an internal credential helper the auth library invokes, never as an agent-callable tool, so none of the `aws` backend's own credential-minting Cerbos guardrails apply here.

### MCP health & credential watcher

`mcp-health-watch` is always on (no flag, no per-backend opt-in): a single supervised loop (`health_watch()` in `vicegerent_mcp.py`) that does two things each poll.

**Workload health.** It polls `thv list`'s own status for every currently-*enabled* workload and fires a macOS notification the first time one drops out of `running`, clearing it once `running` again. This exists because an OAuth-backed remote (`notion`, `linear`) losing its token doesn't just make individual tool calls fail — the whole workload drops out of `thv list`/vMCP's aggregation entirely (`unauthenticated`/`auth_retrying`/`error` in `thv`'s own state, or missing altogether) and stays down until something explicitly restarts it. A static-credential backend (`jira`'s API token, and `kubernetes`/`aws`/`aws_profiles` via `apply:aws_config`) can hit the same "workload just isn't running" state for other reasons too (a crashed container, a bad image pull), so this check is generic across every enabled backend rather than hand-picking OAuth ones.

**AWS credentials.** Whenever the `aws` server is enabled (nothing else here depends on AWS credentials), the same loop also watches that backend's credentials and warns *before* they expire, not only after. Two signals:

- **Expired now** — `aws sts get-caller-identity` (works on any AWS CLI version) fails once the credentials are already expired/unresolvable. This is the guaranteed after-the-fact signal, run every interval against the watched profile.
- **Expiring soon** — while (1) still succeeds, a lookahead warns before the *actionable* re-auth deadline, within the warning window (`--cred-warning-mins`, default 60). Where that deadline comes from depends on the credential type:
  - **SSO** (the watched profile has an `sso_start_url`) — that login's own SSO token `expiresAt` from the local token cache (`~/.aws/sso/cache/*.json`), located by matching the cache file's `startUrl` (robust to botocore's cache-filename hashing). This is what matters for an SSO profile: the short-lived role creds `export-credentials` returns auto-refresh *from* that token, so their sub-hour `Expiration` is cry-wolf, while the token's own expiry is when `aws sso login` (or the operator's login flow) is genuinely needed again. The lookahead is **scoped to the watched profile's login** — it deliberately does *not* scan every cached session, so a stale login left over in another partition (e.g. a near-dead commercial token while GovCloud is fresh) can't fire a warning about credentials you aren't using. A token that carries a `refreshToken` auto-refreshes silently, so its `expiresAt` isn't actionable either — that yields no lookahead and the real re-auth surfaces via signal (1) instead.
  - **Non-SSO** — the watched profile's own `aws configure export-credentials` (AWS CLI v2.9+) `Expiration`, the real hard deadline for a non-refreshing temp-cred source. Static long-lived creds have no `Expiration` (no lookahead), and any export/parse error (an older CLI) simply skips it — it never produces a false "expired".

Which profile both signals track comes from the `aws` server's `cred_watch_profile` param (blank — the default — uses whichever profile `AWS_PROFILE`/`[default]` resolves to, with no `--profile` flag). Set it explicitly to the SSO profile you actually work against so the lookahead scopes to that login rather than an ambiguous default.

Both checks are **detection-only** — the watcher never restarts or refreshes anything itself:

- It never runs a credential-refresh command. However this operator's AWS session gets refreshed (`aws sso login`, an internal access-request tool, whatever) is host-side and often interactive/MFA-gated, which wouldn't work invoked headlessly from a supervised background process anyway — so the notification just says to refresh and re-run `start`, not how.
- It never restarts a workload. `./vicegerent mcp start` already recreates only the workloads whose mounted `~/.aws` content actually changed (see the drift-fingerprint discussion above) and no-ops everything else — so re-running `start` after a notification is the whole fix, with no separate targeted-restart command needed.

Notifications go through `terminal-notifier` (a Homebrew formula, `scripts/host/setup-host-mcp` installs it) rather than plain `osascript`, so they can carry `icon.png` (repo root) via `-contentImage`. That flag is the most macOS actually allows a script to customize: verified live (and confirmed against [node-notifier#71](https://github.com/mikaelbr/node-notifier/issues/71)) that macOS (Catalina+) no longer lets any unsigned script/CLI override the small sending-app icon badge itself — it always shows whichever `.app` bundle actually invoked the notification API (`terminal-notifier.app`'s own icon). Patching that would mean maintaining a separately-bundle-identified private copy of `terminal-notifier.app` (to avoid changing the icon for any other tool on the machine that happens to share the same Homebrew install) — not worth the ongoing fragility for a cosmetic win, so this repo doesn't do it.

Two more gotchas found live while actually watching this fire against a real expired session (both handled once, in the shared `_notify` helper):

- **Repeat notifications need distinct content.** macOS treats a notification with byte-identical title+message as a duplicate of any earlier undismissed one and silently drops it. Since the watcher's `notified` set resets on process start, a fresh process re-detecting the SAME still-ongoing issue (a crash, `autorestart`, or another `start` before the operator has actually fixed anything) would otherwise fire an identical notification that just vanishes. `_notify` appends a timestamp to every message for exactly this reason.
- **A long-lived supervisord can lose its connection to the current GUI login session.** Confirmed live after ~6 hours of uptime: the watcher was correctly detecting the expired session and correctly invoking `terminal-notifier` (traced with `bash -x`) with zero errors, but nothing ever appeared on screen. Restarting just that one supervisord *program* didn't fix it — only fully restarting supervisord *itself* (`./vicegerent mcp stop --keep-workloads && ./vicegerent mcp start`) did. Every process a daemon forks inherits whatever GUI-session bootstrap namespace that daemon had at ITS OWN creation time, no matter how freshly the forked child itself was spawned — so a `terminal-notifier` invocation can report success (exit 0) while actually being delivered into a disconnected session. `_notify` dispatches via `launchctl asuser $(id -u) terminal-notifier ...` instead of calling it directly, which re-executes the command inside the CURRENT session's bootstrap namespace rather than the calling process's own (possibly stale) one — the standard fix for a long-running daemon that needs to reliably reach a logged-in user's GUI session. Verified this doesn't regress normal delivery; the multi-hour staleness itself isn't practically reproducible on demand to re-verify against that exact failure, but this is the documented, standard technique for it.

## Security & trust boundary (read before running on a shared machine)

The host side of this stack trusts the host. Two exposures are inherent to how Docker Desktop + ToolHive work today — know them before running untrusted containers alongside the stack:

- **The vMCP (`127.0.0.1:4483`) is anonymous and reachable from a *sibling Docker container* on the Mac — but NOT from inside the cluster.** The Cilium egress-lock holds the cluster side: only `agentgateway-proxy` gets any host egress, scoped to `192.168.65.0/24:8453`, so even it is denied `:4483`, and the agent sandbox is denied both ports (verified live — every in-cluster path to the vMCP is forced through ghostunnel's mTLS on `:8453`). The residual gap is outside the CNI: on Docker Desktop, `host.docker.internal` resolves to the host loopback for *every* container and the proxy doesn't filter by which container connects, so `docker run alpine wget host.docker.internal:4483/...` reaches the vMCP directly, bypassing ghostunnel. This is a host-trust assumption (don't run a hostile container on this Mac while the stack is up), not an agent-isolation escape. There is no cheap fix in thv v0.33: `incomingAuth` accepts only `anonymous` or `oidc` (no bearer token), the vMCP is TCP-only (no Unix socket), and macOS loopback has only `127.0.0.1` (rebinding to another loopback needs a `sudo` alias). Closing it fully means OIDC incoming auth or upstream vMCP bearer/UDS support.

- **Enabling the `kubernetes` workload widens the trust boundary to the whole `vicegerent` docker network.** That workload runs with `--network vicegerent` (to reach the node's in-network API), and that bridge is flat: any container on it can raw-TCP the kind node's kubelet (`:10250`), apiserver (`:6443`), and every dashboard NodePort (`30119–30128`) by the node's docker IP — bypassing the `127.0.0.1` `extraPortMappings` restriction (which only governs host↔container). None of this is visible to Cilium or the Cerbos guardrail (they only see in-cluster / agentgateway traffic). Backstops: the apiserver is TLS+RBAC-gated and the kubeconfig is `--read-only`; the kubelet has anonymous-auth off (Kind default); the dashboards require basic auth (mandatory — the password Secret is `optional: false`). It is off by default; enabling it is an informed choice.

## Prerequisites

```bash
./vicegerent setup mcp      # brew: thv, ghostunnel, supervisor, rclone, terminal-notifier + Python venv
```

`setup mcp` ends by running `configure`, which prompts for each backend you enable and stores its credentials for you — so the list below is a reference, not a checklist to work through by hand. `./vicegerent mcp doctor` reports what's still missing, scoped to the servers you actually enabled.

```bash
thv secret setup                    # choose 'encrypted' (persists OAuth tokens too)
```

| Backend | thv secrets |
|---|---|
| `kubernetes` | `kubernetes_kubeconfig` (blank at the prompt = auto-generate from the kind cluster) |
| `gitlab` | `gitlab_token` (PAT, api scope), `gitlab_api_url` (e.g. `https://gitlab.example.com/api/v4`) |
| `github` | `github_token` (PAT, repo scope) |
| `tavily` | `tavily_api_key` |
| `firecrawl` | `firecrawl_api_key` |
| `jira` | `jira_url` (e.g. `https://your-domain.atlassian.net`), `jira_username` (account email), `jira_api_token` |
| `grafana` / `grafana_gov` | `grafana_url` + `grafana_service_account_token`, `grafana_gov_url` + `grafana_gov_service_account_token` |
| `alertmanager` / `alertmanager_gov` | `alertmanager_url`, `alertmanager_gov_url` |
| `pagerduty` / `pagerduty_gov` | `pagerduty_user_api_key`, `pagerduty_gov_user_api_key` |
| `elastic` | `elastic_kibana_url` (Agent Builder MCP URL, `https://<kibana-host>/api/agent_builder/mcp`), `elastic_api_key` (read-only, Stack Management > Security > API keys) |
| `notion`, `linear` | none — OAuth in the browser on first run |
| `aws`, `aws_profiles` | none — the read-only `~/.aws` mount |

A `<server>_<param>` name (`gitlab_api_url`, `kubernetes_kubeconfig`, `alertmanager_url`) is a `params[]` entry marked `"secret": true`: params normally live in the disposable `servers-state.json`, and one that's a pain to re-enter opts into the durable `thv` store instead.

`notion` `create-pages` is confined to the **Scratchpad** page by `charts/cerbos-policies/policies/resource_notion.yaml`: a create naming any other parent is denied, with the correct `page_id` in the message. Retarget it via `policy.notion.scratchpadPageId` in your machine `values.yaml` (32 hex, dashes stripped, lowercase), not here.

`./vicegerent setup secrets platform` writes the host ghostunnel mTLS material to `~/.vicegerent/ghostunnel`.

## Subcommands

```
configure     interactively enable/skip each backend and set its secrets
enable KEY    enable one backend (persists; brought up on the next start)
disable KEY   disable one backend (stops it; ToolHive won't run it)
start         bring up enabled workloads + vMCP + ghostunnel (idempotent); --caffeinate keeps macOS awake
stop          shut down the supervised stack AND thv-stop the workloads; --keep-workloads leaves them running
status        workload + supervised-process state (rich table)
logs PROC     tail logs for ghostunnel|vmcp|rclone-s3|mcp-health-watch|supervisord|caffeinate (Ctrl-C to exit)
doctor        check binaries, thv secrets provider + the enabled servers' secrets, kind cluster
```

`stop` stops the workloads by default; the containers survive either way, so OAuth tokens are not re-prompted. `--keep-workloads` only skips the `thv stop`, for when you want the backends reachable while the supervised stack is down. There is one more subcommand, `mcp-health-watch` — supervisord runs it as the fourth supervised process; don't invoke it by hand.

The interactive dashboard (textual) is the top-level `./vicegerent tui`.

```bash
./vicegerent mcp start
./vicegerent mcp status
./vicegerent tui
./vicegerent mcp stop
```

For the full machine lifecycle use the top-level wrapper: `./vicegerent start` resumes the Kind cluster then starts this stack; `./vicegerent stop` reverses it.

## Config + env

`toolhive-servers.json` declares the group, the vMCP port, and the 17 servers (name, run type, package/registry, run flags, env, and thv secret mappings). Overridable env:

```text
THV                     thv binary (default: thv, resolved on PATH)
THV_GROUP               ToolHive group name (default: vicegerent)
VMCP_HOST / VMCP_PORT   vMCP loopback target (default 127.0.0.1:4483)
VMCP_OPTIMIZER          0 disables --optimizer, exposing every tool raw (default: on)
LISTEN                  ghostunnel listen address (default 127.0.0.1:8453)
GHOSTUNNEL_HOST_DIR     host mTLS material (default ~/.vicegerent/ghostunnel)
RCLONE_ADDR             rclone serve s3 listen address (default 127.0.0.1:9899)
RCLONE_S3_HOST_DIR      rclone auth-key material (default ~/.vicegerent/rclone-s3)
RCLONE_SERVE_DIR        directory rclone serves as the Velero bucket (default <repo>/velero-backups)
```

## Runtime state files

```text
~/.vicegerent/mcp/supervisord.conf        # generated supervisord config
~/.vicegerent/mcp/supervisord.pid         # supervisord pid (stale-process detection)
~/.vicegerent/mcp/supervisor.sock         # supervisord control socket
~/.vicegerent/mcp/vmcp-config.json        # generated + validated vMCP config
~/.vicegerent/mcp/vmcp-init.yaml          # raw `thv vmcp init` output, post-processed into vmcp-config.json
~/.vicegerent/mcp/servers-state.json      # which backends are enabled + their non-secret params
~/.vicegerent/mcp/kubeconfig-vicegerent.yaml  # kind --internal kubeconfig (mounted into the k8s workload)
~/.vicegerent/mcp/logs/                   # per-process logs
```

All of it is disposable — delete the directory and the next `start` regenerates everything except `servers-state.json`, which is what `configure`/`enable`/`disable` write and is the one file worth not losing.
