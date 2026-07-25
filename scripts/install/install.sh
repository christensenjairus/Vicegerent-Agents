#!/usr/bin/env bash
# Staged, idempotent (re-)install of the vicegerent platform onto a local Kind
# cluster. The control plane (stage order, chart coords, pinned versions, image
# tags) lives in stages/stages.yaml, and the machine plane
# (clusterVars/agents/egress/models) in a gitignored values.yaml
# copied from values.example.yaml. Every stage runs its actions in order and
# health-gates (helm --wait / kubectl wait) before the next, so a `git pull` +
# re-run delivers upgrades with no gaps.
#
# Secrets are NOT created here — the setup scripts own them as Kubernetes Secrets.
# The platform/agents stages pre-flight the Secrets their workloads block on and
# fail fast with a pointer instead of a 10-minute --wait hang.
#
# Usage: install.sh [flags]
#   -y, --yes            auto-approve every prompt (non-interactive)
#       --values <file>  machine plane values (default: <repo>/values.yaml)
#       --stage <name>   run only this stage
#       --from <name>    run this stage and every stage after it
#   -h, --help           show this help
#
# Env: RECREATE=1     add `helm --force-replace` (delete/recreate on immutable-field conflict)
#      HELM_TIMEOUT   per-release --wait timeout (default 10m)
#      VALUES_FILE    machine plane values (same as --values; the flag wins)
#      DEFAULTS_FILE  default layer laid under it (default: <repo>/values.defaults.yaml)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

VALUES_FILE="${VALUES_FILE:-$REPO_ROOT/values.yaml}"
DEFAULTS_FILE="${DEFAULTS_FILE:-$REPO_ROOT/values.defaults.yaml}"
STAGES_FILE="$REPO_ROOT/stages/stages.yaml"
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"
RECREATE="${RECREATE:-0}"
ASSUME_YES=0
ONLY_STAGE=""
FROM_STAGE=""

# Scratch dir for lib-helper temp files. A single dir the parent owns survives the
# $(...) subshells those helpers run in -- an array appended to inside a subshell
# would never reach this scope -- and never leaks. INT/TERM need their own handler
# because a bare EXIT trap does not run when bash is killed by a signal, and they
# exit rather than return so the stage loop can't resume without its scratch dir.
WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
LOC=()
VALS=()

# shellcheck source=../lib/kube-context.sh
source "$REPO_ROOT/scripts/lib/kube-context.sh"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/helm.sh
source "$SCRIPT_DIR/lib/helm.sh"
# shellcheck source=lib/kubectl.sh
source "$SCRIPT_DIR/lib/kubectl.sh"
# shellcheck source=lib/reconcile.sh
source "$SCRIPT_DIR/lib/reconcile.sh"

need_arg() { [[ $# -ge 2 && "$2" != -* ]] || die "$1 requires an argument"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)   ASSUME_YES=1 ;;
    --values)   need_arg "$@"; VALUES_FILE="$2"; shift ;;
    --stage)    need_arg "$@"; ONLY_STAGE="$2"; shift ;;
    --from)     need_arg "$@"; FROM_STAGE="$2"; shift ;;
    -h|--help)  sed -n '2,24p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[[ -z "$ONLY_STAGE" || -z "$FROM_STAGE" ]] \
  || die "--stage and --from are mutually exclusive (--stage filters first, so the pair always runs nothing)"

if [[ "$RECREATE" == "1" ]]; then
  HELM_UPGRADE_FLAGS=(--wait --force-replace)
else
  HELM_UPGRADE_FLAGS=(--wait --rollback-on-failure)
fi
HELM_UPGRADE_FLAGS+=(--hide-notes)

# --- prerequisites ---------------------------------------------------------
step "Prerequisites"
for cmd in kubectl helm yq git; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is not installed or not on PATH"
done
# The installer uses Helm 4 flags (--rollback-on-failure, --force-replace,
# --hide-notes); a positively-detected Helm 3 fails fast instead of erroring
# mid-stage on an "unknown flag". A parse hiccup falls through rather than block.
helm_major="$(helm version --short 2>/dev/null | sed -E 's/^v?([0-9]+).*/\1/')"
if [[ "$helm_major" =~ ^[0-9]+$ ]] && [[ "$helm_major" -lt 4 ]]; then
  die "Helm 4+ required (found Helm ${helm_major}); upgrade helm"
fi
require_kind_context
[[ -f "$STAGES_FILE" ]] || die "stages file not found: $STAGES_FILE"
[[ -f "$DEFAULTS_FILE" ]] || die "defaults file not found: $DEFAULTS_FILE"
[[ -f "$VALUES_FILE" ]] \
  || die "machine values not found: $VALUES_FILE — copy values.example.yaml to values.yaml and edit it"
kc cluster-info >/dev/null || die "cannot reach cluster on context '$KUBE_CONTEXT'"
require_agent_names
info "Tools present; context '$KUBE_CONTEXT' reachable; using $VALUES_FILE"

# --- dispatch one action ---------------------------------------------------
run_action() {
  local a="$1"
  local type name
  type="$(yq "$a.type" "$STAGES_FILE")"
  name="$(yq "$a.name" "$STAGES_FILE")"
  case "$type" in
    helm)
      helm_remote "$name" \
        "$(yq "$a.repo" "$STAGES_FILE")" \
        "$(yq "$a.chart" "$STAGES_FILE")" \
        "$(yq "$a.version" "$STAGES_FILE")" \
        "$(yq "$a.namespace" "$STAGES_FILE")" \
        "$(yq "$a.values // \"\"" "$STAGES_FILE")" \
        "$(yq "$a.crds // false" "$STAGES_FILE")"
      ;;
    helm-oci)
      local extra_set="" rvp rsk rv
      rvp="$(yq "$a.replicasValuePath // \"\"" "$STAGES_FILE")"
      rsk="$(yq "$a.replicasSetKey // \"\"" "$STAGES_FILE")"
      if [[ -n "$rvp" && -n "$rsk" ]]; then
        rv="$(resolve_value_or_default "$rvp")"
        [[ -n "$rv" ]] && extra_set="${rsk}=${rv}"
      fi
      helm_oci "$name" \
        "$(yq "$a.chart" "$STAGES_FILE")" \
        "$(yq "$a.version" "$STAGES_FILE")" \
        "$(yq "$a.namespace" "$STAGES_FILE")" \
        "$(yq "$a.values // \"\"" "$STAGES_FILE")" \
        "$(yq "$a.crds // false" "$STAGES_FILE")" \
        "$extra_set"
      ;;
    helm-git)
      helm_git "$name" \
        "$(yq "$a.gitRepo" "$STAGES_FILE")" \
        "$(yq "$a.ref" "$STAGES_FILE")" \
        "$(yq "$a.chartPath" "$STAGES_FILE")" \
        "$(yq "$a.namespace" "$STAGES_FILE")" \
        "$(yq "$a.values // \"\"" "$STAGES_FILE")" \
        "$(yq "$a.crds // false" "$STAGES_FILE")"
      ;;
    local)
      helm_local "$name" \
        "$(yq "$a.namespace" "$STAGES_FILE")" \
        "$(yq "$a.machineValues // \"\"" "$STAGES_FILE")" \
        "$(yq "$a.forEach // \"\"" "$STAGES_FILE")"
      ;;
    kubectl-k)
      kubectl_k "$name" \
        "$(yq "$a.path" "$STAGES_FILE")" \
        "$(yq "$a.gate // \"\"" "$STAGES_FILE")" \
        "$(yq "$a.waitResource // \"\"" "$STAGES_FILE")" \
        "$(yq "$a.waitNamespace // \"\"" "$STAGES_FILE")"
      ;;
    *) die "unknown action type '$type' for action '$name'" ;;
  esac
}

# --- stage loop ------------------------------------------------------------
# A misspelled --stage/--from would otherwise match nothing, run zero stages, and
# still print "Install complete." -- which reads as a successful restore on the
# disaster-recovery path docs/setup.md points at.
stage_names="$(yq -r '.stages[].name' "$STAGES_FILE")"
for flag_name in ONLY_STAGE FROM_STAGE; do
  want="${!flag_name}"
  [[ -z "$want" ]] && continue
  grep -qxF "$want" <<<"$stage_names" \
    || die "no such stage '$want'; stages are: $(tr '\n' ' ' <<<"$stage_names")"
done

stage_count="$(yq '.stages | length' "$STAGES_FILE")"
reached=0
[[ -z "$FROM_STAGE" ]] && reached=1

for ((si = 0; si < stage_count; si++)); do
  sname="$(yq ".stages[$si].name" "$STAGES_FILE")"

  if [[ -n "$ONLY_STAGE" && "$sname" != "$ONLY_STAGE" ]]; then continue; fi
  if [[ "$reached" == 0 ]]; then
    [[ "$sname" == "$FROM_STAGE" ]] && reached=1 || continue
  fi

  step "STAGE: $sname"
  case "$sname" in
    controllers) preflight_controller_secrets ;;
    platform)    preflight_platform_secrets ;;
    agents)      preflight_agent_secrets ;;
  esac

  action_count="$(yq ".stages[$si].actions | length" "$STAGES_FILE")"
  for ((ai = 0; ai < action_count; ai++)); do
    run_action ".stages[$si].actions[$ai]"
  done

  [[ "$sname" == "agents" ]] && reconcile_agents
done

echo
info "${G}Install complete.${N}"
echo "Inspect with:"
echo "  helm --kube-context $KUBE_CONTEXT list -A"
echo "  kubectl --context $KUBE_CONTEXT get pods -A"
