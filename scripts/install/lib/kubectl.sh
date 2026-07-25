#!/usr/bin/env bash
# kubectl-based action runners: `kubectl apply -k` for pinned-ref kustomize overlays
# and the vendored csi-driver-host-path tree, plus the two readiness gates that
# replace Flux's `wait: true` barrier.

# kubectl_k <name> <path> <gate> <waitResource> <waitNamespace>
#   gate=established  wait every applied CRD to reach Established (CRD stages)
#   gate=rollout      wait <waitResource> rollout in <waitNamespace> (controller stages)
# Server-side apply avoids the last-applied-annotation size limit that large CRDs
# (gateway-api) blow past, and --force-conflicts lets a re-apply take ownership.
kubectl_k() {
  local name="$1" path="$2" gate="${3:-}" waitResource="${4:-}" waitNamespace="${5:-}"
  step "kubectl apply -k ${path}  (${name})"
  # Kustomize refetches remote-ref bases (external-snapshotter) on every apply, and
  # a transient git-fetch timeout there must not abort the whole install. The
  # server-side apply is idempotent, so retry a few times before giving up.
  local out attempt=1
  while :; do
    if out="$(kc apply -k "$REPO_ROOT/$path" --server-side --force-conflicts -o name)"; then
      break
    fi
    if [[ "$attempt" -ge 3 ]]; then
      die "kubectl apply -k ${path} failed after 3 attempts"
    fi
    warn "kubectl apply -k ${path} failed (attempt ${attempt}/3); retrying in 5s"
    attempt=$((attempt + 1))
    sleep 5
  done
  echo "$out"
  case "$gate" in
    established)
      local r
      while IFS= read -r r; do
        [[ "$r" == customresourcedefinition* ]] || continue
        info "wait Established: ${r}"
        kc wait --for=condition=Established "$r" --timeout=2m
      done <<< "$out"
      ;;
    rollout)
      [[ -n "$waitResource" ]] || return 0
      info "rollout status: ${waitResource} (ns=${waitNamespace})"
      kc -n "$waitNamespace" rollout status "$waitResource" --timeout=5m
      ;;
  esac
}
