#!/usr/bin/env bash
# Shared helpers for the staged installer: logging, prompts, tool/context guards,
# machine-plane (values.yaml) slicing, and the secret pre-flight.
#
# Sourced by install.sh (which owns the globals REPO_ROOT, KUBE_CONTEXT,
# STAGES_FILE, VALUES_FILE, ASSUME_YES, and the $WORKDIR scratch dir).

if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[0;33m'; R=$'\033[0;31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
info() { echo "${G}•${N} $*"; }
step() { echo; echo "${B}== $* ==${N}"; }
warn() { echo "${Y}!${N} $*" >&2; }
die()  { echo "${R}ERROR:${N} $*" >&2; exit 1; }

confirm() {
  echo
  echo "${Y}CHANGE:${N} $*"
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

# Write the values.yaml `egress:` block, re-rooted to top level, to a temp file
# consumable as `charts/egress-proxy` values. Prints the temp file path.
mv_slice_egress() {
  local out; out="$(mktemp_f)"
  yq '.egress' "$VALUES_FILE" > "$out"
  printf '%s' "$out"
}

# Write the values.yaml `agents[<idx>]` entry, re-rooted to top level, to a temp
# file consumable as `charts/agent` values. Prints the temp file path.
mv_slice_agent() {
  local idx="$1" out; out="$(mktemp_f)"
  yq ".agents[$idx]" "$VALUES_FILE" > "$out"
  printf '%s' "$out"
}

# Write the values.defaults.yaml `egress:` block, re-rooted to top level, to a
# temp file layered UNDER the machine egress slice. Prints the temp file path.
defaults_slice_egress() {
  local out; out="$(mktemp_f)"
  yq '.egress' "$DEFAULTS_FILE" > "$out"
  printf '%s' "$out"
}

# Write the values.defaults.yaml `agents[0]` default template, re-rooted to top
# level, to a temp file layered UNDER each machine agent slice. Prints the path.
defaults_slice_agent() {
  local out; out="$(mktemp_f)"
  yq '.agents[0]' "$DEFAULTS_FILE" > "$out"
  printf '%s' "$out"
}

# Fail fast if a Secret the next stage's workloads block on is absent, so the
# operator gets a one-line pointer instead of a 10-minute `helm --wait` hang.
# Secrets are owned by the setup scripts; the installer never creates them.
require_secret() {
  local ns="$1" name="$2" hint="$3"
  kc -n "$ns" get secret "$name" >/dev/null 2>&1 \
    || die "missing Secret ${ns}/${name} — run: ${hint}"
}

preflight_platform_secrets() {
  local hint="./vicegerent setup secrets platform"
  require_secret agentgateway-system vicegerent-mcp-client "$hint"
  require_secret agentgateway-system ghostunnel-server "$hint"
  require_secret egress-proxy       egress-proxy-ca      "$hint"
  require_secret agent-sandbox      egress-proxy-ca-cert "$hint"
  info "Platform secrets present."
}

preflight_agent_secrets() {
  local n
  while IFS= read -r n; do
    [[ -n "$n" ]] || continue
    require_secret agent-sandbox "${n}-secrets" "./vicegerent setup secrets agent ${n}"
  done < <(yq '.agents[].name' "$VALUES_FILE")
  info "Agent secrets present."
}
