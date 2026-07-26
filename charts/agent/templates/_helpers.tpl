{{- define "vicegerent-agent.name" -}}
{{- .Release.Name -}}
{{- end -}}

{{- /* Shared coding-agent instruction: web_search/WebSearch/WebFetch are disabled
      in codex, claude-code, and opencode because they're server-side and bypass the sealed
      egress proxy. Single source of truth for codex's developer_instructions,
      claude-code's seeded CLAUDE.md, and opencode's seeded AGENTS.md — keep them in sync by editing only here. */ -}}
{{- define "vicegerent-agent.webSearchInstructions" -}}
WebSearch/web_search and WebFetch are disabled — both are server-side tools that bypass the sealed egress proxy. For web search, curl $SEARXNG_URL/search?q=<query>&format=json instead.
{{- end -}}

{{- /* Shared coding-agent instruction: every external capability is exposed through the
      single vmcp MCP server, discovered by tool search rather than a fixed named list.
      Single source of truth for hermes-agent's SOUL.md (via vicegerent-agent.environment),
      codex's developer_instructions, claude-code's seeded CLAUDE.md, and opencode's seeded AGENTS.md. */ -}}
{{- define "vicegerent-agent.vmcpToolDiscovery" -}}
Every external capability you need (Kubernetes, GitLab, Notion, monitoring, etc.) is exposed through the single `vmcp` MCP server's tool search, not a fixed list you already know. Before telling the user an action isn't possible, exhaustively search vmcp (vary your query wording) — most capabilities already exist there and just need the right search terms.
{{- end -}}

{{- /* Shared coding-agent instruction: use .worktrees correctly for any repo with a
      persistent clone under /workspace. Single source of truth for hermes-agent's
      SOUL.md (via vicegerent-agent.environment), codex's developer_instructions,
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
Mnemosyne is the only memory store; use it for facts, preferences, and insights. Its memories are shared by Hermes, Claude Code, Codex, and OpenCode, so never maintain a harness-specific memory store. The same is true of skills: Hermes owns the canonical tree at `$HERMES_HOME/skills`, published to every harness through its native skill-discovery path; skills created or updated from any harness must remain useful across all four.
{{- if .Values.obsidian.vaultPath }} `OBSIDIAN_VAULT_PATH` points to the shared git-synced Obsidian vault at `{{ .Values.obsidian.vaultPath }}`. Use the `obsidian` skill for vault reads, writes, and search. Keep the vault, skills, and Mnemosyne as separate systems: durable human-reviewable knowledge belongs in the vault, reusable procedures belong in skills, and Mnemosyne should hold short recall facts or pointers to vault paths rather than copied vault content.{{- end }}
{{- end -}}

{{- define "vicegerent-agent.environment" -}}
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
- **Tools are an allowlist, but discovery is search, not memory.**
  {{ include "vicegerent-agent.vmcpToolDiscovery" . }}
  Only conclude a tool isn't wired up after exhausting vmcp search — that's a real gap, not a transient error.
- **The filesystem is mostly ephemeral.** Only the mounted data and
  workspace volumes persist; everything else resets when the pod restarts.
  Clone repos and keep git worktrees under `/workspace` — it's the
  persistent volume for git repos and survives pod restarts; anywhere
  else is wiped.
- **Use `.worktrees` correctly for any repo already cloned under `/workspace`.**
  {{ include "vicegerent-agent.worktreeDiscipline" . }}

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
Use `claude-code`, `codex`, or `opencode` for medium/large tasks and all code reviews — don't inline large coding work.
Pick the model that fits the task: heavier reasoning for complex/design work, lighter/faster for quick fixes and alternatives.

# Memory
- {{ include "vicegerent-agent.sharedKnowledge" . }}
- **Repo knowledge**: also add a terse bullet to `AGENTS.md` in your next PR.
{{- if .Values.obsidian.vaultPath }}

# Obsidian vault
- `OBSIDIAN_VAULT_PATH` is set to `{{ .Values.obsidian.vaultPath }}` — a git-synced Obsidian vault.
  It is the durable, version-controlled knowledge base: prefer it over ad-hoc notes for anything
  meant to survive indefinitely and be reviewed/edited by a human.
- Treat the vault as an [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
  bundle unless the user's existing vault says otherwise: one concept per markdown file, YAML
  frontmatter (`type` required), `index.md` per directory for progressive disclosure, bundle-relative
  links (`[text](/topic/concept.md)`), a `log.md` changelog.
- Use the `obsidian` skill for reads/writes/search. Don't `git commit`/`git push` after every single
  edit — batch them. Commit and push at the end of a work session, or at least once a day if the
  session runs long, using the already-configured SSH key. The vault directory persists across pod
  restarts either way; committing regularly is about off-site durability and human visibility, not
  preventing data loss.
- Keep skills (`$HERMES_HOME/skills/`) and the vault as separate systems — don't move or symlink
  skills into the vault. Skills carry their own curator lifecycle (usage telemetry, staleness,
  archiving) that assumes a skill-shaped directory, not an OKF bundle; cross-reference a skill by
  name from a vault concept instead of merging the two.
- Mnemosyne stays the fast-recall layer: store short pointers to vault concept paths there, not
  copies of vault content, so the two stores don't drift out of sync.
{{- end }}

# Expectations
- Workspace layout: one full clone per repo under /workspace/<repo-name> (git blame/log -p work inline).
- Be thorough in your debugging. Find a smoking gun before suggesting a fix.
- Never guess or assume, always back up statements with data.
- You are designed to be AUTONOMOUS. Run issues to completion or until you get stuck.
- When MCP servers misbehave, stop execution and tell the user.
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
providers:
{{- if .Values.providers.anthropic.enabled }}
  anthropic:
    name: Agentgateway-Anthropic
    api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/anthropic
    key_env: ANTHROPIC_API_KEY
    transport: anthropic_messages
{{- end }}
{{- if .Values.providers.openai.enabled }}
  openai:
    name: Agentgateway-OpenAI
    api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai/v1
    key_env: OPENAI_API_KEY
    transport: responses
{{- end }}
{{- if .Values.providers.deepseek.enabled }}
  deepseek:
    name: Agentgateway-DeepSeek
    api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/deepseek/v1
    key_env: DEEPSEEK_API_KEY
    transport: chat_completions
    models:
      - {{ .Values.providers.deepseek.model }}
{{- end }}
{{- if .Values.providers.zai.enabled }}
  zai:
    name: Agentgateway-Zai
    api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/zai/api/paas/v4
    key_env: ZAI_API_KEY
    transport: chat_completions
    models:
      - {{ .Values.providers.zai.model }}
{{- end }}
model_catalog:
  excluded_providers:
    - anthropic
    - openai-api
    - deepseek
    - zai
{{- end -}}

{{- /* Platform-wide operational defaults; overridable per-agent via values.config.
      $primaryProvider is what every assistant-facing default (model.provider, delegation,
      auxiliary.*) points at when an agent doesn't override it -- first enabled among
      anthropic/openai/deepseek/zai, in that order. Fails loudly if every provider is
      disabled -- an agent needs at least one. */ -}}
{{- define "vicegerent-agent.defaultConfig" -}}
{{- $primaryProvider := "" -}}
{{- $primaryModel := "" -}}
{{- $primaryAuxiliaryModel := "" -}}
{{- if .Values.providers.anthropic.enabled }}{{- $primaryProvider = "anthropic" -}}{{- $primaryModel = .Values.providers.anthropic.model -}}{{- $primaryAuxiliaryModel = .Values.providers.anthropic.auxiliaryModel -}}
{{- else if .Values.providers.openai.enabled }}{{- $primaryProvider = "openai" -}}{{- $primaryModel = .Values.providers.openai.model -}}{{- $primaryAuxiliaryModel = .Values.providers.openai.auxiliaryModel -}}
{{- else if .Values.providers.deepseek.enabled }}{{- $primaryProvider = "deepseek" -}}{{- $primaryModel = .Values.providers.deepseek.model -}}{{- $primaryAuxiliaryModel = .Values.providers.deepseek.auxiliaryModel -}}
{{- else if .Values.providers.zai.enabled }}{{- $primaryProvider = "zai" -}}{{- $primaryModel = .Values.providers.zai.model -}}{{- $primaryAuxiliaryModel = .Values.providers.zai.auxiliaryModel -}}
{{- else }}{{- fail "vicegerent-agent: every provider (anthropic/openai/deepseek/zai) is disabled in values.providers -- at least one must be enabled" -}}
{{- end }}
model:
  default: {{ $primaryModel }}
  provider: {{ $primaryProvider }}
  context_length: {{ .Values.tuning.contextLength }}
  persist_switch_by_default: false
model_aliases:
{{- if .Values.providers.anthropic.enabled }}
  haiku:
    model: {{ .Values.providers.anthropic.auxiliaryModel }}
    provider: anthropic
  sonnet:
    model: {{ .Values.providers.anthropic.model }}
    provider: anthropic
  opus:
    model: {{ .Values.harnesses.claudeCode }}
    provider: anthropic
{{- end }}
{{- if .Values.providers.openai.enabled }}
  gpt-5:
    model: {{ .Values.providers.openai.model }}
    provider: openai
{{- end }}
{{- if .Values.providers.deepseek.enabled }}
  deepseek:
    model: {{ .Values.providers.deepseek.model }}
    provider: deepseek
{{- end }}
{{- if .Values.providers.zai.enabled }}
  zai:
    model: {{ .Values.providers.zai.model }}
    provider: zai
{{- end }}
{{- $fp := .Values.failover.provider -}}
{{- if $fp }}
{{- if not (has $fp (list "anthropic" "openai" "deepseek" "zai")) }}{{- fail (printf "failover.provider %q must be one of anthropic/openai/deepseek/zai" $fp) -}}{{- end -}}
{{- if index .Values.providers $fp "enabled" }}
{{- $fpModel := .Values.failover.model -}}
{{- if not $fpModel }}{{- $fpModel = index .Values.providers $fp "model" -}}{{- end -}}
{{- $gw := "http://agentgateway-proxy.agentgateway-system.svc.cluster.local" -}}
{{- $fpBase := "" -}}
{{- $fpKey := "" -}}
{{- if eq $fp "anthropic" }}{{- $fpBase = printf "%s/mnemosyne-anthropic/v1" $gw -}}{{- $fpKey = "ANTHROPIC_API_KEY" -}}
{{- else if eq $fp "openai" }}{{- $fpBase = printf "%s/openai/v1" $gw -}}{{- $fpKey = "OPENAI_API_KEY" -}}
{{- else if eq $fp "deepseek" }}{{- $fpBase = printf "%s/deepseek/v1" $gw -}}{{- $fpKey = "DEEPSEEK_API_KEY" -}}
{{- else if eq $fp "zai" }}{{- $fpBase = printf "%s/zai/api/paas/v4" $gw -}}{{- $fpKey = "ZAI_API_KEY" -}}
{{- end }}
fallback_providers:
  # provider is the REAL provider name, not "custom". Both honor explicit
  # base_url identically (verified: resolve_provider_client passes
  # explicit_base_url through for every provider name, so the gateway route
  # below is preserved either way), but "custom" makes the entry unbillable:
  # usage_pricing.resolve_billing_route() maps custom/local to
  # billing_mode="unknown" unconditionally, so EVERY failover session recorded
  # cost_status="unknown" and showed no cost in the Slack footer regardless of
  # what the price table contained. Naming the provider restores attribution.
  - provider: {{ $fp }}
    model: {{ $fpModel }}
    base_url: {{ $fpBase }}
    key_env: {{ $fpKey }}
{{- end }}
{{- end }}
compression:
  threshold: {{ .Values.tuning.compressionThreshold }}
prompt_caching:
  cache_ttl: {{ .Values.tuning.cacheTtl }}
mcp_servers:
  agentburn:
    command: /opt/hermes/.venv/bin/agentburn
    args: [mcp]
    env:
      HERMES_HOME: /opt/data
  vmcp:
    url: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp
    timeout: {{ .Values.tuning.vmcp.timeoutSeconds }}
    connect_timeout: {{ .Values.tuning.vmcp.connectTimeoutSeconds }}
agent:
  max_turns: {{ .Values.tuning.maxTurns }}
  gateway_timeout: {{ .Values.tuning.gatewayTimeoutSeconds }}
  api_max_retries: {{ .Values.tuning.apiMaxRetries }}
  reasoning_effort: {{ .Values.tuning.reasoningEffort }}
  reasoning_overrides:
    {{ .Values.providers.openai.model }}: none
{{- if .Values.providers.deepseek.enabled }}
    {{ .Values.providers.deepseek.model }}: high
{{- end }}
{{- if .Values.providers.zai.enabled }}
    {{ .Values.providers.zai.model }}: high
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
        fields: [model, effort, context_pct, cost, duration]
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
{{- $agentConfig = mergeOverwrite $default $agentConfig -}}
{{- $mandatory := include "vicegerent-agent.mandatoryConfig" . | fromYaml -}}
{{- $merged := mergeOverwrite $agentConfig $mandatory -}}
{{- /* model.* is a connection independent of providers.*; re-derive it too so it can't diverge. */ -}}
{{- $activeProviderName := "anthropic" -}}
{{- if (kindIs "map" $merged.model) -}}
{{- if $merged.model.provider -}}
{{- $activeProviderName = $merged.model.provider -}}
{{- end -}}
{{- end -}}
{{- if not (hasKey $merged.providers $activeProviderName) -}}
{{- fail (printf "vicegerent-agent: model.provider %q has no matching entry in providers — must be one of anthropic, openai, deepseek, zai, or an agent-defined entry under providers.*" $activeProviderName) -}}
{{- end -}}
{{- $activeProvider := index $merged.providers $activeProviderName -}}
{{- $lockedModel := dict "provider" $activeProviderName "base_url" $activeProvider.api "key_env" $activeProvider.key_env "api_mode" $activeProvider.transport -}}
{{- $merged = mergeOverwrite $merged (dict "model" $lockedModel) -}}
{{- $merged | toYaml -}}
{{- end -}}
