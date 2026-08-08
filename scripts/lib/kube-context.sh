#!/usr/bin/env bash
# Centralized kube-context resolution for the vicegerent CLI and its scripts.
#
# Defaults to kind-vicegerent; the undocumented VICEGERENT_USE_CURRENT_CONTEXT escape
# hatch targets the active kubectl context instead, but the result must still start with
# 'kind-' or this aborts. require_kind_context sets the global KUBE_CONTEXT, or exits 1 --
# call it at statement scope (never inside $(...)), since it may exit.
# shellcheck source=cli-ui.sh
if ! declare -F ui_error >/dev/null 2>&1; then
  _vicegerent_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck disable=SC1091 # Resolved beside this file at runtime.
  source "$_vicegerent_lib_dir/cli-ui.sh"
  unset _vicegerent_lib_dir
fi

require_kind_context() {
  if [ -n "${VICEGERENT_USE_CURRENT_CONTEXT:-}" ]; then
    KUBE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
    if [ -z "$KUBE_CONTEXT" ]; then
      ui_error "VICEGERENT_USE_CURRENT_CONTEXT is set, but kubectl has no active context."
      ui_command "kubectl config use-context kind-<cluster>" >&2
      exit 1
    fi
  else
    KUBE_CONTEXT="kind-vicegerent"
  fi
  case "$KUBE_CONTEXT" in
    kind-*) : ;;
    *)
      ui_error "Refusing to target non-Kind context '$KUBE_CONTEXT'; vicegerent only operates on local Kind clusters."
      ui_info "The context name must start with 'kind-'." >&2
      ui_command "kubectl config use-context kind-<cluster>" >&2
      exit 1 ;;
  esac
}

# Kind names the node (docker) container backing a context '<cluster>-control-plane',
# where <cluster> is the context minus its 'kind-' prefix. Call after require_kind_context.
kind_node_container() {
  printf '%s\n' "${KUBE_CONTEXT#kind-}-control-plane"
}
