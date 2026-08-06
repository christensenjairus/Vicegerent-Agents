{{- define "vicegerent-agent.name" -}}
{{- .Release.Name -}}
{{- end -}}

{{- /* Coding-harness-only instruction: Hermes uses the proxied web_search tool. */ -}}
{{- define "vicegerent-agent.webSearchInstructions" -}}
WebSearch/web_search and WebFetch are disabled — both are server-side tools that bypass the sealed egress proxy. For web search, curl $SEARXNG_URL/search?q=<query>&format=json instead.
{{- end -}}

{{- /* Shared coding-agent instruction: every external capability is exposed through the
      single vmcp MCP server, discovered by tool search rather than a fixed named list.
      Single source of truth for hermes-agent's SOUL.md,
      codex's developer_instructions, claude-code's seeded CLAUDE.md, and opencode's seeded AGENTS.md. */ -}}
{{- define "vicegerent-agent.vmcpToolDiscovery" -}}
Every external capability you need (Kubernetes, GitLab, Notion, monitoring, etc.) is exposed through the single `vmcp` MCP server's tool search, not a fixed list you already know. Before telling the user an action isn't possible, exhaustively search vmcp (vary your query wording) — most capabilities already exist there and just need the right search terms.
{{- end -}}

{{- define "vicegerent-agent.vmcpParallelToolCalls" -}}
When several vMCP calls are independent, issue them together in the same assistant response in batches of up to eight so the harness can execute them concurrently for speed and efficiency. Keep dependent calls sequential, and do not assume other MCP servers are safe for concurrent access.
{{- end -}}

{{- define "vicegerent-agent.claudeVmcpParallelToolCalls" -}}
For independent vMCP backend operations in Claude Code, put up to eight entries in one `mcp__vmcp__batch_call_tool` call after discovering the target tools. Do not issue several `mcp__vmcp__call_tool` calls for the batch; Claude Code serializes that generic write-capable tool.
{{- end -}}

{{- /* Shared coding-agent instruction: use .worktrees correctly for any repo with a
      persistent clone under /workspace. Single source of truth for hermes-agent's
      SOUL.md, codex's developer_instructions,
      claude-code's seeded CLAUDE.md, and opencode's seeded AGENTS.md — a wrong-worktree edit was observed live wasting
      most of an hour of agent runtime (edits landed in the primary clone instead of
      the assigned .worktrees/<branch>, and a full-repo validation script then scanned
      unrelated sibling worktrees and failed on their content). Also covers: keeping
      terminal() cwd sticky to the verified worktree for the rest of a task, and
      checking merge status before reusing an existing worktree for a new task (a
      false "not merged" read from git merge-base --is-ancestor once caused a stale,
      already-merged worktree to be reused and edited before the mistake was caught). */ -}}
{{- define "vicegerent-agent.worktreeDiscipline" -}}
When working on a dedicated branch in a repo that already has a persistent clone under `/workspace/<repo>`, use `git worktree add .worktrees/<branch>` off that clone — never a second clone, and never edit directly in the primary clone once you're on a task branch. Before your FIRST file edit in any session, confirm with `pwd` and `git branch --show-current` that you are actually inside the assigned `.worktrees/<branch>` directory, not the primary clone — both look like valid checkouts and nothing errors immediately if you're in the wrong one. This matters especially for full-repo validation scripts (`pre-commit run --all-files`, custom `validate.sh` globs): run from the primary clone, they also scan sibling `.worktrees/` content and can fail on unrelated in-progress work, which looks like a broken repo but is actually a location bug. Once verified, `cd` into that worktree as your first shell/terminal call for the task (not just a one-time `pwd`/`git branch` check) — every subsequent shell command without an explicit working-directory override inherits that cwd, keeping `git status`/`pre-commit`/build commands scoped correctly without re-specifying the path each time; this only fixes shell cwd, since file read/write/patch/search tools take their own explicit path argument and are unaffected by shell cwd (a wrong-path mistake there is a separate failure mode — double-check the literal path, not the shell state). Re-verify `pwd` before resuming work in the original tree after any point where you changed directory elsewhere. Before reusing an existing `.worktrees/<branch>` directory for a *new* task, confirm its branch isn't already merged first (`git log --oneline origin/main | grep <branch-or-commit>`, or check the merge/pull request's own `state`/`merged_at` via its API — `git merge-base --is-ancestor` is unreliable here since merges often land as merge/squash commits with a different SHA than the branch tip); if it's already merged, remove the stale worktree and create a fresh one off `origin/main` rather than editing on top of a merged base.
{{- end -}}

{{- /* Shared durable-knowledge instruction for all four harnesses. Mnemosyne is
      native to Hermes and exposed as a local MCP server to the coding harnesses;
      the implementation differs, but the store and operating policy do not. */ -}}
{{- define "vicegerent-agent.sharedKnowledge" -}}
Mnemosyne is the only memory store; use it for facts, preferences, and insights. Its memories are shared by Hermes, Claude Code, Codex, and OpenCode, so never maintain a harness-specific memory store. The same is true of skills: Hermes owns the canonical tree at `$HERMES_HOME/skills`, published to every harness through its native skill-discovery path; skills created or updated from any harness must remain useful across all four. Skills are shared procedural memory: when work yields a reusable workflow, corrects an existing procedure, or uncovers a non-obvious pitfall, create or update a skill rather than leaving the learning only in chat. Read a skill before modifying it, prefer the canonical tree over harness-local copies, and keep skills portable across all four harnesses. Do not create skills for one-off task progress.
{{- if .Values.obsidian.vaultPath }} `OBSIDIAN_VAULT_PATH` points to the shared git-synced Obsidian vault at `{{ .Values.obsidian.vaultPath }}`. The vault is an Open Knowledge Format (OKF) bundle; before reading or writing it, consult its root `index.md` to locate and read the vault's complete OKF specification, then follow that local spec for concept frontmatter, bundle-relative Markdown links, indexes, and changelogs. Use the `obsidian` skill for vault reads, writes, and search, but the vault's own OKF spec overrides generic Obsidian conventions. After medium-to-large vault changes, commit and push the vault so the work is durable and visible to other users. Treat the vault's `vicegerent` branch as its primary branch and push there as you would normally push to `main`, because agents cannot push to the protected `main` branch. Keep the vault, skills, and Mnemosyne as separate systems: durable human-reviewable knowledge belongs in the vault, reusable procedures belong in skills, and Mnemosyne should hold short recall facts or pointers to vault paths rather than copied vault content.{{- end }}
{{- end -}}

{{- /* Shared repository-authorship instruction for Hermes and every standalone coding harness. */ -}}
{{- define "vicegerent-agent.neutralAuthorship" -}}
Keep repository authorship neutral. Use the repository's configured git identity without supplementing it, and never add model, harness, or agent attribution to commit messages or trailers (including `Co-Authored-By`, `Generated-By`, or phrases such as "authored by Opus via Claude Code"). Do not claim credit in branch names, pull or merge request titles and descriptions, review comments, or other repository metadata. Describe only the change and its verification in the project's normal, bland voice.
{{- end -}}

{{- /* Shared forge-workflow expectation for Hermes and every standalone coding harness. */ -}}
{{- define "vicegerent-agent.draftPullRequestExpectation" -}}
All pull requests and merge requests are forcibly kept as drafts by the platform. This is expected.
{{- end -}}

{{- /* The common prompt shared by Hermes and every standalone coding harness. */ -}}
{{- define "vicegerent-agent.sharedSystemPrompt" -}}
{{ include "vicegerent-agent.vmcpToolDiscovery" . | trim }}

{{ include "vicegerent-agent.vmcpParallelToolCalls" . | trim }}

{{ include "vicegerent-agent.worktreeDiscipline" . | trim }}

{{ include "vicegerent-agent.sharedKnowledge" . | trim }}

{{ include "vicegerent-agent.neutralAuthorship" . | trim }}

{{ include "vicegerent-agent.draftPullRequestExpectation" . | trim }}
{{- end -}}

{{- /* Standalone harnesses differ from Hermes only in their web tooling. */ -}}
{{- define "vicegerent-agent.codingHarnessSystemPrompt" -}}
{{ include "vicegerent-agent.webSearchInstructions" . | trim }}

{{ include "vicegerent-agent.sharedSystemPrompt" . | trim }}
{{- end -}}

{{- define "vicegerent-agent.hermesInstructions" -}}
# Environment
You run inside a sealed agent sandbox: a non-root container on a
locked-down Kubernetes cluster, installed by a staged Helm script. The platform is
defined in the `vicegerent-agents` repo
(gitlab.hahomelabs.com/jchristensen/vicegerent-agents) — that repo is where
your own capabilities, models, tools, and limits are configured.

## Limitations to expect
- **Egress is sealed.** Most direct outbound TCP is dropped — no raw HTTP/HTTPS,
  no package managers, no direct API calls. Approved channels only:
  - **`web_search`** — internet lookups via the in-cluster SearXNG proxy.
  - **MCP servers** — all external integrations (GitLab, Kubernetes, Notion, web scraping, etc.).
  - **agentgateway** — all model API calls; don't call providers directly.
  - **`git` over SSH (port 22)** — the only approved direct TCP outside the cluster.
  If none cover your need, tell the user what to add.
- **No cluster credentials by default.** Your service-account token is not
  mounted; you cannot read Secrets or mutate the cluster unless a specific
  capability was granted to you in the repo.
- **The filesystem is mostly ephemeral.** Only the mounted data and
  workspace volumes persist; everything else resets when the pod restarts.
  Clone repos and keep git worktrees under `/workspace` — it's the
  persistent volume for git repos and survives pod restarts; anywhere
  else is wiped.

## When you hit a wall
If a task is blocked by the sandbox itself — a missing tool, a sealed
endpoint you legitimately need, absent credentials, or a denied action —
**say so plainly and tell the user what access would unblock you.** Name the
specific capability (e.g. "I need the `foo` MCP tool added" or "the gateway
needs a route to bar.example.com"). The user can change the repo to grant
it. Don't silently fail, fabricate a result, or burn turns retrying
something the environment structurally prevents. Surfacing the gap is the
correct, expected move — the human is your path to expanding what you can do.

## Masked content
Tool results and context sometimes contain `<masked>`, where a redaction layer
(egress-proxy, mcp-cerbos-shim, or agentgateway's own prompt guard) scrubbed a secret or
PII before it reached you. If a `<masked>` value is what's actually blocking the task,
say so and tell the user — don't guess the hidden value, retry to route around the
redaction, or quietly give up. The user decides whether it was a false positive or gets
you what you need another way.

# Coding agents
Handle coding tasks directly by default. Use `claude-code`, `codex`, or `opencode` only for genuinely large PRs, when the user explicitly requests delegation, or for an independent code review; do not delegate ordinary fixes, features, or refactors.
Choose the harness based on both the task and the model that produced the work. For delegated implementation, prefer a harness backed by the same provider or model family as the current Hermes model so the approaches stay aligned. For independent PR review, cross providers: use Codex with an OpenAI model to review Anthropic-produced work, Claude Code with an Anthropic model to review OpenAI-produced work, and otherwise choose an available reviewer from a different provider.
Within that constraint, pick the model that fits the task: heavier reasoning for complex/design work, lighter/faster for quick fixes and alternatives.

# Memory
- Add to `AGENTS.md` only when a task establishes a durable, repository-wide contributor rule that future work must follow and that cannot be enforced by code or validation. Do not record one-off implementation details, task-specific root causes, or behavior already captured by tests, validators, or subsystem documentation.

# Expectations
- Workspace layout: one full clone per repo under /workspace/<repo-name> (git blame/log -p work inline).
- Be thorough in your debugging. Find a smoking gun before suggesting a fix.
- Never guess or assume, always back up statements with data.
- You are designed to be AUTONOMOUS. Run issues to completion or until you get stuck.
- When MCP servers misbehave, stop execution and tell the user.
{{- end -}}

{{- define "vicegerent-agent.providerOrder" -}}
["anthropic", "openai", "deepseek", "zai"]
{{- end -}}

{{- /* Single source of truth for every Hermes provider connection. Values use
      upstream names (notably openai); id is the canonical Hermes runtime slug. */ -}}
{{- define "vicegerent-agent.providerCatalog" -}}
anthropic:
  enabled: {{ .Values.providers.anthropic.enabled }}
  id: anthropic
  name: Agentgateway-Anthropic
  api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/anthropic
  mnemosyneApi: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mnemosyne-anthropic/v1
  keyEnv: ANTHROPIC_API_KEY
  baseEnv: ANTHROPIC_BASE_URL
  transport: anthropic_messages
  model: {{ .Values.providers.anthropic.model }}
  auxiliaryModel: {{ .Values.providers.anthropic.auxiliaryModel }}
  mnemosyneModel: {{ .Values.providers.anthropic.mnemosyneModel }}
  moaModels: {{ .Values.providers.anthropic.moaModels | toJson }}
  models: {{ list .Values.providers.anthropic.model .Values.providers.anthropic.auxiliaryModel .Values.providers.anthropic.mnemosyneModel .Values.providers.anthropic.moaModels.balanced .Values.providers.anthropic.moaModels.frontier .Values.harnesses.claudeCode | uniq | toJson }}
  aliases:
    haiku: {{ .Values.providers.anthropic.auxiliaryModel }}
    sonnet: {{ .Values.providers.anthropic.model }}
    opus: {{ .Values.harnesses.claudeCode }}
openai:
  enabled: {{ .Values.providers.openai.enabled }}
  id: openai-api
  name: Agentgateway-OpenAI
  api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai/v1
  mnemosyneApi: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai/v1
  keyEnv: OPENAI_API_KEY
  baseEnv: OPENAI_BASE_URL
  transport: codex_responses
  model: {{ .Values.providers.openai.model }}
  auxiliaryModel: {{ .Values.providers.openai.auxiliaryModel }}
  mnemosyneModel: {{ .Values.providers.openai.mnemosyneModel }}
  moaModels: {{ .Values.providers.openai.moaModels | toJson }}
  models: {{ list .Values.providers.openai.model .Values.providers.openai.auxiliaryModel .Values.providers.openai.mnemosyneModel .Values.providers.openai.moaModels.balanced .Values.providers.openai.moaModels.frontier .Values.harnesses.codex .Values.harnesses.openCode "gpt-5.6-sol" "gpt-5.6-terra" "gpt-5.6-luna" | uniq | toJson }}
  aliases:
    gpt-5: {{ .Values.providers.openai.model }}
    sol: gpt-5.6-sol
    terra: gpt-5.6-terra
    luna: gpt-5.6-luna
deepseek:
  enabled: {{ .Values.providers.deepseek.enabled }}
  id: deepseek
  name: Agentgateway-DeepSeek
  api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/deepseek/v1
  mnemosyneApi: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/deepseek/v1
  keyEnv: DEEPSEEK_API_KEY
  baseEnv: DEEPSEEK_BASE_URL
  transport: chat_completions
  model: {{ .Values.providers.deepseek.model }}
  auxiliaryModel: {{ .Values.providers.deepseek.auxiliaryModel }}
  mnemosyneModel: {{ .Values.providers.deepseek.mnemosyneModel }}
  moaModels: {{ .Values.providers.deepseek.moaModels | toJson }}
  models: {{ list .Values.providers.deepseek.model .Values.providers.deepseek.auxiliaryModel .Values.providers.deepseek.mnemosyneModel .Values.providers.deepseek.moaModels.balanced .Values.providers.deepseek.moaModels.frontier | uniq | toJson }}
  aliases:
    deepseek: {{ .Values.providers.deepseek.model }}
zai:
  enabled: {{ .Values.providers.zai.enabled }}
  id: zai
  name: Agentgateway-Zai
  api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/zai/api/paas/v4
  mnemosyneApi: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/zai/api/paas/v4
  keyEnv: ZAI_API_KEY
  baseEnv: ZAI_BASE_URL
  transport: chat_completions
  model: {{ .Values.providers.zai.model }}
  auxiliaryModel: {{ .Values.providers.zai.auxiliaryModel }}
  mnemosyneModel: {{ .Values.providers.zai.mnemosyneModel }}
  moaModels: {{ .Values.providers.zai.moaModels | toJson }}
  models: {{ list .Values.providers.zai.model .Values.providers.zai.auxiliaryModel .Values.providers.zai.mnemosyneModel .Values.providers.zai.moaModels.balanced .Values.providers.zai.moaModels.frontier | uniq | toJson }}
  aliases:
    zai: {{ .Values.providers.zai.model }}
{{- end -}}

{{- /* Locked provider connectivity every agent must have; wins on merge conflicts.
      Gated on values.providers.<name>.enabled so an agent can opt a provider out
      entirely -- disabling one drops it from renderedConfig's providers map, so
      model.provider can't dangle. model_catalog.excluded_providers is a static list
      that hides Hermes's built-in providers whose canonical slug would otherwise
      collide with our Agentgateway-* providers in the /model picker: the built-in
      activates on the placeholder <PROVIDER>_API_KEY=none the sandbox sets for
      agentgateway and claims the slug first. Excluded slugs are the built-in
      canonical names (openai-api, zai) or the shared config key (anthropic,
      deepseek) -- see MR !612. */ -}}
{{- define "vicegerent-agent.mandatoryConfig" -}}
{{- $catalog := include "vicegerent-agent.providerCatalog" . | fromYaml -}}
{{- $providerOrder := include "vicegerent-agent.providerOrder" . | fromJsonArray -}}
providers:
{{- range $name := $providerOrder }}
{{- $provider := index $catalog $name -}}
{{- if $provider.enabled }}
  {{ $provider.id }}:
    name: {{ $provider.name }}
    api: {{ $provider.api }}
    key_env: {{ $provider.keyEnv }}
    transport: {{ $provider.transport }}
    models: {{ $provider.models | toJson }}
{{- end }}
{{- end }}
model_catalog:
  excluded_providers:
{{- range $name := $providerOrder }}
    - {{ index $catalog $name "id" }}
{{- end }}
{{- end -}}

{{- /* Platform-wide operational defaults; overridable per-agent via values.config.
      $primaryProvider is what every assistant-facing default (model.provider, delegation,
      auxiliary.*) points at when an agent doesn't override it -- first enabled among
      anthropic/openai/deepseek/zai, in that order. Fails loudly if every provider is
      disabled -- an agent needs at least one. */ -}}
{{- define "vicegerent-agent.defaultConfig" -}}
{{- $catalog := include "vicegerent-agent.providerCatalog" . | fromYaml -}}
{{- $providerOrder := include "vicegerent-agent.providerOrder" . | fromJsonArray -}}
{{- $primaryProvider := "" -}}
{{- $primaryModel := "" -}}
{{- $primaryAuxiliaryModel := "" -}}
{{- range $name := $providerOrder -}}
{{- $provider := index $catalog $name -}}
{{- if and (not $primaryProvider) $provider.enabled -}}
{{- $primaryProvider = $provider.id -}}
{{- $primaryModel = $provider.model -}}
{{- $primaryAuxiliaryModel = $provider.auxiliaryModel -}}
{{- end -}}
{{- end -}}
{{- if not $primaryProvider }}{{- fail "vicegerent-agent: every provider (anthropic/openai/deepseek/zai) is disabled in values.providers -- at least one must be enabled" -}}
{{- end }}
model:
  default: {{ $primaryModel }}
  provider: {{ $primaryProvider }}
  context_length: {{ .Values.tuning.contextLength }}
  persist_switch_by_default: false
{{- $aliases := dict -}}
{{- range $name := $providerOrder }}
{{- $provider := index $catalog $name -}}
{{- if $provider.enabled }}
{{- range $alias, $model := $provider.aliases }}
{{- $_ := set $aliases $alias (dict "model" $model "provider" $provider.id "base_url" $provider.api) -}}
{{- end }}
{{- end }}
{{- end }}
model_aliases:
{{ $aliases | toYaml | nindent 2 }}
{{- $aggregatorName := .Values.moa.aggregator.provider -}}
{{- if not (hasKey $catalog $aggregatorName) }}{{- fail (printf "vicegerent-agent: moa.aggregator.provider %q must be one of anthropic/openai/deepseek/zai" $aggregatorName) -}}{{- end -}}
{{- $aggregator := index $catalog $aggregatorName -}}
{{- if not $aggregator.enabled }}{{- fail (printf "vicegerent-agent: moa.aggregator.provider %q is disabled in values.providers" $aggregatorName) -}}{{- end -}}
{{- $presets := dict -}}
{{- range $preset := list "default" "frontier" }}
{{- $tier := $preset -}}
{{- if eq $preset "default" }}{{- $tier = "balanced" -}}{{- end -}}
{{- $references := list -}}
{{- range $name := $providerOrder }}
{{- $provider := index $catalog $name -}}
{{- if $provider.enabled }}
{{- $references = append $references (dict "provider" $provider.id "model" (index $provider.moaModels $tier)) -}}
{{- end }}
{{- end }}
{{- $aggregatorModel := index $.Values.moa.aggregator.models $preset -}}
{{- if not (has $aggregatorModel $aggregator.models) }}{{- fail (printf "vicegerent-agent: moa.aggregator.models.%s %q is not declared by provider %q" $preset $aggregatorModel $aggregatorName) -}}{{- end -}}
{{- $_ := set $presets $preset (dict "reference_models" $references "aggregator" (dict "provider" $aggregator.id "model" $aggregatorModel) "enabled" true) -}}
{{- end }}
moa:
  default_preset: default
  presets:
{{ $presets | toYaml | nindent 4 }}
{{ $fp := .Values.failover.provider -}}
{{- if $fp }}
{{- if not (hasKey $catalog $fp) }}{{- fail (printf "vicegerent-agent: failover.provider %q must be one of anthropic/openai/deepseek/zai" $fp) -}}{{- end -}}
{{- $fallback := index $catalog $fp -}}
{{- if not $fallback.enabled }}{{- fail (printf "vicegerent-agent: failover.provider %q is disabled in values.providers" $fp) -}}{{- end -}}
{{- $fpModel := .Values.failover.model -}}
{{- if not $fpModel }}{{- $fpModel = $fallback.model -}}{{- end -}}
fallback_providers:
  - provider: {{ $fallback.id }}
    model: {{ $fpModel }}
    base_url: {{ $fallback.api }}
    key_env: {{ $fallback.keyEnv }}
    api_mode: {{ $fallback.transport }}
{{- end }}
compression:
  threshold: {{ .Values.tuning.compressionThreshold }}
prompt_caching:
  cache_ttl: {{ .Values.tuning.cacheTtl }}
mcp_servers:
  agentburn:
    command: /opt/hermes/.venv/bin/agentburn
    args: [mcp]
    supports_parallel_tool_calls: false
    env:
      HERMES_HOME: /opt/data
  vmcp:
    url: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp
    timeout: {{ .Values.tuning.vmcp.timeoutSeconds }}
    connect_timeout: {{ .Values.tuning.vmcp.connectTimeoutSeconds }}
    supports_parallel_tool_calls: true
    elicitation:
      enabled: false
agent:
  max_turns: {{ .Values.tuning.maxTurns }}
  gateway_timeout: {{ .Values.tuning.gatewayTimeoutSeconds }}
  api_max_retries: {{ .Values.tuning.apiMaxRetries }}
  reasoning_effort: {{ .Values.tuning.reasoningEffort }}
  reasoning_overrides:
{{- range $name := $providerOrder }}
{{- $provider := index $.Values.providers $name }}
{{- if $provider.enabled }}
    {{ $provider.model }}: {{ $provider.reasoningEffort }}
{{- end }}
{{- end }}
  disabled_toolsets:
    - computer_use
    - tts
    - browser
    - image_gen
platform_toolsets:
  slack:
    - hermes-slack
    - kanban
toolsets:
  - hermes-cli
  - kanban
kanban:
  dispatch_in_gateway: false
delegation:
  provider: {{ $primaryProvider }}
  model: {{ $primaryModel }}
  orchestrator_enabled: true
  max_spawn_depth: {{ .Values.tuning.maxSpawnDepth }}
auxiliary:
  vision:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  title_generation:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  approval:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  compression:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
    context_length: {{ .Values.tuning.contextLength }}
  web_extract:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  triage_specifier:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  kanban_decomposer:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  curator:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
  monitor:
    provider: {{ $primaryProvider }}
    model: {{ $primaryAuxiliaryModel }}
tool_loop_guardrails:
  hard_stop_enabled: {{ .Values.tuning.toolLoopGuardrails.hardStopEnabled }}
  warn_after:
    exact_failure: {{ .Values.tuning.toolLoopGuardrails.warnAfterExactFailure }}
    same_tool_failure: {{ .Values.tuning.toolLoopGuardrails.warnAfterSameToolFailure }}
  hard_stop_after:
    exact_failure: {{ .Values.tuning.toolLoopGuardrails.hardStopAfterExactFailure }}
    same_tool_failure: {{ .Values.tuning.toolLoopGuardrails.hardStopAfterSameToolFailure }}
display:
  skin: slate
  streaming: true
  show_cost: true
  timestamps: true
  tool_progress: verbose
  platforms:
    slack:
      tool_progress: false
      interim_assistant_messages: false
      long_running_notifications: false
      runtime_footer:
        enabled: true
        fields: [model, effort, context_pct, cost, latency]
      file_mutation_verifier: false
      memory_notifications: "off"
      busy_ack_enabled: false
approvals:
  mode: smart
command_allowlist: []
# Auto-accept: every agent entrypoint here is headless and cannot answer the
# hook consent prompt.
hooks_auto_accept: true
hooks:
  post_tool_call:
    - matcher: skill_manage
      command: /usr/local/bin/sync-shared-skills.sh
      timeout: 30
    - matcher: skill_manage
      command: /usr/local/bin/snapshot-skills.sh
      timeout: 30
skills:
  # Claude's root only: codex ships a vendored .system/ tree and opencode's
  # skills are compiled in, so those roots import vendor skills, not the user's.
  external_dirs:
    - /opt/data/.claude/skills
checkpoints:
  enabled: true
clarify:
  timeout: {{ .Values.tuning.clarifyTimeoutSeconds }}
timezone: {{ .Values.timezone }}
terminal:
  cwd: /workspace
  persistent_shell: true
  # 'auto' would rewrite tool-subprocess HOME to {HERMES_HOME}/home in containers, splitting it from the gateway's.
  home_mode: real
web:
  search_backend: searxng
memory:
  provider: mnemosyne
  memory_enabled: false
  user_profile_enabled: false
  mnemosyne:
    auto_sleep: true
context:
  engine: lcm
lsp:
  enabled: true
  install_strategy: manual
slack:
  require_mention: true
  strict_mention: true
plugins:
  enabled:
    - disk-cleanup
    - rtk-rewrite
    - security-guidance
  disabled:
    - google_meet
    - spotify
    - teams_pipeline
    - raft-platform
    - web/exa
    - web/parallel
    - web/brave_free
    - web/tavily
    - web/firecrawl
    - web/ddgs
    - web/xai
    - image_gen/fal
    - image_gen/krea
    - image_gen/openai
    - image_gen/openai-codex
    - image_gen/xai
    - video_gen/fal
    - video_gen/xai
    - browser/browser_use
    - browser/browserbase
    - browser/firecrawl
{{- end -}}

{{- define "vicegerent-agent.renderedConfig" -}}
{{- $agentConfig := .Values.config | deepCopy -}}
{{- if not (kindIs "map" $agentConfig) -}}
{{- fail (printf "vicegerent-agent: .Values.config must be a YAML map (got %s)" (kindOf $agentConfig)) -}}
{{- end -}}
{{- $default := include "vicegerent-agent.defaultConfig" . | fromYaml -}}
{{- $lockedDefaults := $default | deepCopy -}}
{{- $agentConfig = mergeOverwrite $default $agentConfig -}}
{{- $mandatory := include "vicegerent-agent.mandatoryConfig" . | fromYaml -}}
{{- $merged := $agentConfig -}}
{{- $_ := set $merged "providers" $mandatory.providers -}}
{{- $_ := set $merged "model_catalog" $mandatory.model_catalog -}}
{{- $_ := set $merged "model_aliases" $lockedDefaults.model_aliases -}}
{{- $_ := set $merged "moa" $lockedDefaults.moa -}}
{{- if hasKey $lockedDefaults "fallback_providers" -}}
{{- $_ := set $merged "fallback_providers" $lockedDefaults.fallback_providers -}}
{{- else -}}
{{- $_ := unset $merged "fallback_providers" -}}
{{- end -}}
{{- if and (hasKey $merged "custom_providers") $merged.custom_providers -}}
{{- fail "vicegerent-agent: config.custom_providers is forbidden because model traffic must use Agentgateway" -}}
{{- end -}}
{{- $_ := unset $merged "custom_providers" -}}
{{- /* Lock every route's connection fields after applying agent model choices. */ -}}
{{- $activeProviderName := "anthropic" -}}
{{- if (kindIs "map" $merged.model) -}}
{{- if $merged.model.provider -}}
{{- $activeProviderName = $merged.model.provider -}}
{{- end -}}
{{- end -}}
{{- if not (hasKey $merged.providers $activeProviderName) -}}
{{- fail (printf "vicegerent-agent: model.provider %q is not an enabled Agentgateway provider" $activeProviderName) -}}
{{- end -}}
{{- $activeProvider := index $merged.providers $activeProviderName -}}
{{- $lockedModel := dict "provider" $activeProviderName "base_url" $activeProvider.api "key_env" $activeProvider.key_env "api_mode" $activeProvider.transport -}}
{{- $merged = mergeOverwrite $merged (dict "model" $lockedModel) -}}
{{- if not (kindIs "map" $merged.delegation) }}{{- fail "vicegerent-agent: config.delegation must be a YAML map" -}}{{- end -}}
{{- if not (kindIs "map" $merged.auxiliary) }}{{- fail "vicegerent-agent: config.auxiliary must be a YAML map" -}}{{- end -}}
{{- $routes := dict "delegation" $merged.delegation -}}
{{- range $name, $route := $merged.auxiliary -}}
{{- if not (kindIs "map" $route) }}{{- fail (printf "vicegerent-agent: config.auxiliary.%s must be a YAML map" $name) -}}{{- end -}}
{{- $_ := set $routes (printf "auxiliary.%s" $name) $route -}}
{{- end -}}
{{- range $label, $route := $routes -}}
{{- $providerName := default $activeProviderName $route.provider -}}
{{- if not (hasKey $merged.providers $providerName) }}{{- fail (printf "vicegerent-agent: config.%s.provider %q is not an enabled Agentgateway provider" $label $providerName) -}}{{- end -}}
{{- $provider := index $merged.providers $providerName -}}
{{- $_ := set $route "provider" $providerName -}}
{{- $_ := set $route "base_url" $provider.api -}}
{{- $_ := set $route "key_env" $provider.key_env -}}
{{- $_ := set $route "api_mode" $provider.transport -}}
{{- end -}}
{{- $merged | toYaml -}}
{{- end -}}
