#!/usr/bin/env bash
# Helm action runners for the staged installer. Every release is applied with
# `helm upgrade --install --wait` so a re-run reconciles idempotently and Helm's
# own release-manifest tracking prunes objects dropped from a chart.
#
# _do_helm reads two caller-set globals - LOC (chart-location args, e.g. the chart
# ref plus --repo/--version, or a local/cloned path) and VALS (values -f args) -
# so apply_crds and the upgrade share one exact location+values definition.

# Helm never upgrades CRDs shipped in a chart's crds/ directory (install-only, by
# design), so we server-side-apply those CRDs before the release upgrade. `helm
# show crds` emits ONLY the crds/-dir CRDs - NOT CRDs a chart renders from templates/ (e.g. tetragon), which
# Helm creates and upgrades with the release itself and would refuse to adopt if we
# pre-applied them ("invalid ownership metadata"). No-op for charts with no crds/ dir.
apply_crds() {
  local name="$1" out err; out="$(mktemp_f)"; err="$(mktemp_f)"
  # A registry/network/auth failure here used to leave an empty file that the
  # -s guard skipped in silence, upgrading the release against stale CRDs.
  helm show crds "${LOC[@]}" > "$out" 2>"$err" \
    || die "helm show crds failed for ${name}: $(tr '\n' ' ' < "$err")"
  if [[ -s "$out" ]]; then
    info "apply_crds: server-side applying crds/-dir CRDs for ${name}"
    kc apply --server-side --force-conflicts -f "$out"
  fi
}

_do_helm() {
  local name="$1" namespace="$2" crds="$3"
  [[ "$crds" == "true" ]] && apply_crds "$name"
  info "helm upgrade --install ${name} (ns=${namespace})"
  # ${VALS[@]+…}: an empty VALS (the crds-only chart has no values file) is an
  # "unbound variable" under bash-3.2/macOS set -u; the +alt guard expands to nothing.
  helmc upgrade --install "$name" "${LOC[@]}" \
    -n "$namespace" --create-namespace \
    "${HELM_UPGRADE_FLAGS[@]}" --timeout "$HELM_TIMEOUT" \
    ${VALS[@]+"${VALS[@]}"}
}

_vals_from_file() {
  local values="$1"
  VALS=()
  if [[ -n "$values" ]]; then VALS=(-f "$REPO_ROOT/stages/values/$values"); fi
}

# helm_remote <name> <repo> <chart> <version> <namespace> <values> <crds> [contextValue]
# contextValue maps a chart setting to the selected Kind cluster name, keeping
# cluster-scoped controller identities aligned when testing another Kind context.
helm_remote() {
  local name="$1" repo="$2" chart="$3" version="$4" namespace="$5" values="$6" crds="$7" context_value="${8:-}"
  LOC=("$chart" --repo "$repo" --version "$version")
  _vals_from_file "$values"
  [[ -n "$context_value" ]] && VALS+=(--set "$context_value=${KUBE_CONTEXT#kind-}")
  _do_helm "$name" "$namespace" "$crds"
}

# helm_oci <name> <chart-oci-ref> <version> <namespace> <values> <crds> [extraSet]
# extraSet (optional): a single "key=value" appended as `--set` after the values
# file, e.g. the cerbos stage's machine-configurable replicaCount (upstream
# charts have no values.yaml layering here, unlike the local charts).
helm_oci() {
  local name="$1" chart="$2" version="$3" namespace="$4" values="$5" crds="$6" extraSet="${7:-}"
  LOC=("$chart" --version "$version")
  _vals_from_file "$values"
  [[ -n "$extraSet" ]] && VALS+=(--set "$extraSet")
  _do_helm "$name" "$namespace" "$crds"
}

# helm_git <name> <gitRepo> <ref> <chartPath> <namespace> <values> <crds>
# Clones the repo at a pinned tag (Renovate tracks the ref in stages.yaml) and
# installs the chart from the checkout - no vendored chart source in this repo.
helm_git() {
  local name="$1" gitRepo="$2" ref="$3" chartPath="$4" namespace="$5" values="$6" crds="$7"
  local dir; dir="$(mktemp_d)"
  info "git clone ${gitRepo} @ ${ref}"
  git clone --depth 1 --branch "$ref" "$gitRepo" "$dir" >/dev/null 2>&1 \
    || die "failed to clone ${gitRepo} at ${ref}"
  LOC=("$dir/$chartPath")
  _vals_from_file "$values"
  _do_helm "$name" "$namespace" "$crds"
}

# helm_local <name> <namespace> <machineValues> <forEach>
# In-repo charts fed from values.defaults.yaml layered UNDER the gitignored
# values.yaml (machine wins). machineValues=full passes both whole files;
# forEach=agents installs one release per agents[] entry (release name = entry
# name), layering agentDefaults under each machine entry and
# injecting dashboard.index=<i> so the NodePort derives to 30119+i.
helm_local() {
  local name="$1" namespace="$2" machineValues="$3" forEach="$4"
  local chartdir="$REPO_ROOT/charts/$name"

  if [[ "$forEach" == "agents" ]]; then
    local count; count="$(yq '.agents | length' "$VALUES_FILE")"
    [[ "$count" -gt 0 ]] || { warn "no agents in values.yaml; skipping ${name}"; return 0; }
    [[ "$count" -le 10 ]] \
      || die "at most 10 agents supported (dashboard NodePort pool 30119-30128 in kind-config.yaml); found $count"
    local dvf i aname vf
    dvf="$(defaults_slice_agent)"
    for ((i = 0; i < count; i++)); do
      aname="$(yq -r ".agents[$i].name" "$VALUES_FILE")"
      vf="$(mv_slice_agent "$i")"
      LOC=("$chartdir"); VALS=(-f "$dvf" -f "$vf" --set "dashboard.index=$i")
      _do_helm "$aname" "$namespace" false
    done
    return 0
  fi

  case "$machineValues" in
    full)   LOC=("$chartdir"); VALS=(-f "$DEFAULTS_FILE" -f "$VALUES_FILE")
            # platform's promptGuard partial compiles its regexes from the canonical
            # secret-patterns.json (same file the shim embeds); inject it at render.
            [[ "$name" == "platform" || "$name" == "egress-proxy" ]] && VALS+=(--set-file "secretPatterns=$REPO_ROOT/images/mcp-cerbos-shim/internal/server/secret-patterns.json") ;;
    *)      die "action '$name': a local chart needs machineValues: full or forEach: agents" ;;
  esac
  _do_helm "$name" "$namespace" false
}
