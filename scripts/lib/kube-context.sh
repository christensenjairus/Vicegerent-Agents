#!/usr/bin/env bash
# Centralized kube-context resolution for the vicegerent CLI and its scripts.
#
# vicegerent only ever operates on a local Kind cluster. By default it targets the
# canonical `kind-vicegerent` context, so a normal single-cluster user never has to
# know about kubectl contexts or remember to select one — `./vicegerent install` just
# works regardless of whatever ambient context happens to be active. The undocumented
# VICEGERENT_USE_CURRENT_CONTEXT escape hatch (set it to any non-empty value) instead
# targets whatever context kubectl is currently on, for a developer juggling several
# Kind clusters at once (e.g. a throwaway test cluster beside the real one); switch
# between them with `kubectl config use-context`.
#
# Either way the resolved context must be a Kind context (name starts with 'kind-'),
# so a stray or production context can never be targeted. The env var is inherited by
# every child process the CLI exec's, so this one function is the single source of
# truth across all bash entrypoints.
#
# require_kind_context sets the global KUBE_CONTEXT, or aborts (exit 1). Call it at
# statement scope (never inside $(...)), since it may exit.
require_kind_context() {
  if [ -n "${VICEGERENT_USE_CURRENT_CONTEXT:-}" ]; then
    KUBE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
    if [ -z "$KUBE_CONTEXT" ]; then
      echo "ERROR: VICEGERENT_USE_CURRENT_CONTEXT is set but kubectl has no active context. Select one: kubectl config use-context kind-<cluster>" >&2
      exit 1
    fi
  else
    KUBE_CONTEXT="kind-vicegerent"
  fi
  case "$KUBE_CONTEXT" in
    kind-*) : ;;
    *)
      echo "ERROR: refusing to target non-Kind context '$KUBE_CONTEXT': vicegerent only operates on local Kind clusters (context must start with 'kind-'). Switch with: kubectl config use-context kind-<cluster>" >&2
      exit 1 ;;
  esac
}

# Kind names the node (docker) container backing a context '<cluster>-control-plane',
# where <cluster> is the context minus its 'kind-' prefix. Call after require_kind_context.
kind_node_container() {
  printf '%s\n' "${KUBE_CONTEXT#kind-}-control-plane"
}
