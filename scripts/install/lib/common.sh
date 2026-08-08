#!/usr/bin/env bash
# Shared helpers for the staged installer: logging, prompts, tool/context guards,
# machine-plane (values.yaml) slicing, and the secret pre-flight.
#
# Sourced by install.sh (which owns the globals REPO_ROOT, KUBE_CONTEXT,
# DEFAULTS_FILE, VALUES_FILE, ASSUME_YES, and the $WORKDIR scratch dir).

# shellcheck source=../../lib/cli-ui.sh
source "$REPO_ROOT/scripts/lib/cli-ui.sh"

info() { ui_info "$@"; }
step() { ui_section "$@"; }
warn() { ui_warn "$@"; }
die()  { ui_error "$@"; exit 1; }

confirm() {
  echo
  echo "${UI_YELLOW}${UI_BOLD}Change${UI_RESET}  $*"
  if [[ "$ASSUME_YES" == "1" ]]; then
    echo "  (auto-approved via --yes)"
    return 0
  fi
  local ans
  read -r -p "  Proceed? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

kc()   { kubectl --context "$KUBE_CONTEXT" "$@"; }
helmc() { helm --kube-context "$KUBE_CONTEXT" "$@"; }

# Temp files/dirs live under install.sh's $WORKDIR and are removed with it on EXIT.
mktemp_f() { mktemp "$WORKDIR/f.XXXXXX"; }
mktemp_d() { mktemp -d "$WORKDIR/d.XXXXXX"; }

# Write the values.yaml `agents[<idx>]` entry, re-rooted to top level, to a temp
# file consumable as `charts/agent` values. Prints the temp file path.
mv_slice_agent() {
  local idx="$1" out; out="$(mktemp_f)"
  yq ".agents[$idx]" "$VALUES_FILE" > "$out"
  printf '%s' "$out"
}

# Write the values.defaults.yaml `agentDefaults` map, re-rooted to top
# level, to a temp file layered UNDER each machine agent slice. Prints the path.
defaults_slice_agent() {
  local out; out="$(mktemp_f)"
  yq '.agentDefaults' "$DEFAULTS_FILE" > "$out"
  printf '%s' "$out"
}

# Resolve a dotted value path from the machine values.yaml, falling back to
# values.defaults.yaml when the machine file doesn't set it (mirrors the Helm
# `-f defaults -f machine` layering for charts that have no such layering of
# their own, e.g. an upstream OCI chart passed only a single -f). Prints the
# resolved scalar, or "" if neither file sets it.
resolve_value_or_default() {
  local path="$1" v
  v="$(yq eval "$path // \"\"" "$VALUES_FILE")"
  [[ -n "$v" && "$v" != "null" ]] || v="$(yq eval "$path // \"\"" "$DEFAULTS_FILE")"
  [[ "$v" != "null" ]] || v=""
  printf '%s' "$v"
}

# Fail fast if a Secret the next stage's workloads block on is absent, so the
# operator gets a one-line pointer instead of a 10-minute `helm --wait` hang.
# Secrets are owned by the setup scripts; the installer never creates them.
require_secret() {
  local ns="$1" name="$2" hint="$3"
  kc -n "$ns" get secret "$name" >/dev/null 2>&1 \
    || die "missing Secret ${ns}/${name} — run: ${hint}"
}

preflight_controller_secrets() {
  local hint="./vicegerent setup secrets platform"
  require_secret cerbos mcp-cerbos-shim-self-token "$hint"
  info "Controller secrets present."
}

preflight_platform_secrets() {
  local hint="./vicegerent setup secrets platform"
  require_secret agentgateway-system vicegerent-mcp-client "$hint"
  require_secret agentgateway-system ghostunnel-server "$hint"
  require_secret egress-proxy       egress-proxy-ca      "$hint"
  require_secret agent-sandbox      egress-proxy-ca-cert "$hint"
  require_secret searxng            searxng-secret       "$hint"
  info "Platform secrets present."
}

# Every agent entry names its own Helm release, Secret, and every resource in it.
# An omitted name reaches helm as the literal string "null" (yq's rendering of the
# missing key) and installs a release called `null`; the documented `''` default
# reaches it as empty and fails with helm's own opaque name check. Checked once up
# front so a bad machine file fails before any stage runs, not at the last one.
require_agent_names() {
  local count i n
  count="$(yq '.agents | length' "$VALUES_FILE")"
  for ((i = 0; i < count; i++)); do
    n="$(yq -r ".agents[$i].name // \"\"" "$VALUES_FILE")"
    [[ -n "$n" && "$n" != "null" ]] \
      || die "agents[$i].name is required in $VALUES_FILE (it names the Helm release and every resource)"
  done
}

validate_values_schema() {
  local legacy
  legacy="$(yq -r '[
    (has("clusterVars")),
    ((.egress // {}) | has("apexWildcardDomains")),
    ((.egress // {}) | has("exactOnlyDomains")),
    ((.egress // {}) | has("internalAllowlistCIDRs")),
    ((.egress // {}) | has("replicaCount")),
    ([.agents[]? | has("networkAllowlist")] | any),
    ([.agents[]? | ((.directEgress.ssh // {}) | has("fqdn") or has("cnameChain"))] | any),
    ([.agents[]? | (.storage // {}) | has("gitrepos")] | any),
    ([.agents[]? | (.tuning // {}) | has("gatewayTimeout") or has("clarifyTimeout")] | any),
    ([.agents[]? | (.tuning.vmcp // {}) | has("timeout") or has("connectTimeout")] | any),
    ([.agents[]? | .config? | type == "!!str"] | any)
  ] | any' "$VALUES_FILE")"
  [[ "$legacy" != "true" ]] || die "$VALUES_FILE uses the retired values schema; migrate it from clusterVars/networkAllowlist/directEgress.ssh.fqdn/comma-separated egress fields to policy/directEgress.ssh.hosts/list fields (see values.example.yaml)"
  python3 "$REPO_ROOT/scripts/validate-model-backend-alignment.py" \
    --defaults "$DEFAULTS_FILE" "$VALUES_FILE" \
    || die "$VALUES_FILE enables an agent provider without its platform model backend"
}

preflight_agent_secrets() {
  local n
  while IFS= read -r n; do
    require_secret agent-sandbox "${n}-secrets" "./vicegerent setup secrets agent ${n}"
  done < <(yq -r '.agents[].name' "$VALUES_FILE")
  info "Agent secrets present."
}

# True if the host's `thv vmcp serve` process is running. The agent pod's
# wait-deps initContainer (charts/agent/templates/_sandbox.tpl) blocks startup
# until it can reach vMCP through agentgateway, so an agents-stage install with
# vMCP down leaves the pod stuck in Init until the operator runs `vicegerent
# start`.
vmcp_running() {
  pgrep -f "vmcp serve" >/dev/null 2>&1
}

# Yellow heads-up, only shown when vMCP isn't up, right before the agents stage
# installs the agent chart -- so the operator sees it before the pod goes
# Pending/Init instead of discovering it later via `kubectl get pods`.
warn_vmcp_down() {
  vmcp_running && return 0
  warn "vMCP does not appear to be running on the host (no 'vmcp serve' process found)."
  warn "The agent pod's wait-deps init step blocks until vMCP is reachable, so it may not finish starting."
  warn "Run './vicegerent start' (or './vicegerent mcp start') to bring up the host MCP stack."
}
