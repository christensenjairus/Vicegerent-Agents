#!/usr/bin/env bash
# Print the dashboard basic-auth username + password for an agent.
#
# Each agent's random password lives in its own Kubernetes Secret
# (<agent>-secrets, key `password`) in the agent-sandbox namespace, mounted only
# into that agent's pod. No salt, no derivation, no shared secret - one agent
# cannot read or compute another's credentials.
#
#   username = <agent name>
#   password = Secret agent-sandbox/<agent>-secrets key `password`
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/cli-ui.sh
source "$SCRIPT_DIR/../lib/cli-ui.sh"
# shellcheck source=../lib/kube-context.sh
source "$SCRIPT_DIR/../lib/kube-context.sh"

NAMESPACE="${AGENT_DASHBOARD_NAMESPACE:-agent-sandbox}"
DEFAULT_NODEPORT="${AGENT_DASHBOARD_NODEPORT:-30119}"

usage() {
  ui_error "Usage: $0 <agent-name>"
  ui_info "Prints the dashboard basic-auth username, password, and URL for that agent."
  exit 2
}

[ "$#" -eq 1 ] || usage
name="$1"
[ -n "$name" ] || usage
name="$(echo "$name" | tr '[:upper:]' '[:lower:]')"

command -v kubectl >/dev/null 2>&1 || { ui_error "kubectl is required."; exit 1; }
require_kind_context
CONTEXT_ARG=(--context "$KUBE_CONTEXT")

password="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get secret "${name}-secrets" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"
[ -n "$password" ] || {
  ui_error "No password in Secret ${NAMESPACE}/${name}-secrets."
  ui_command "./vicegerent setup secrets"
  exit 1
}

SERVICE="${AGENT_DASHBOARD_SERVICE:-${name}-dashboard}"
node_port="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get svc "$SERVICE" -o jsonpath='{.spec.ports[?(@.name=="dashboard")].nodePort}' 2>/dev/null || true)"
[ -n "$node_port" ] || node_port="$DEFAULT_NODEPORT"

# The nodePort is the port INSIDE the Kind node container. Kind publishes it to the
# host through an extraPortMapping whose hostPort need not equal the containerPort, so
# resolve the real host-published port from the node container rather than assuming
# they match.
host_port="$node_port"
if command -v docker >/dev/null 2>&1; then
  mapped="$(docker port "$(kind_node_container)" "${node_port}/tcp" 2>/dev/null | head -n1 || true)"
  if [ -n "$mapped" ]; then
    host_port="${mapped##*:}"
  else
    ui_warn "NodePort ${node_port} is not published to the host by Kind; the dashboard may be unreachable at this URL. Add an extraPortMapping in scripts/install/kind-config.yaml and recreate the cluster."
  fi
fi

ui_header "Dashboard credentials"
ui_key_value "Agent" "$name"
ui_key_value "Username" "$name"
ui_key_value "Password" "$password"
ui_key_value "URL" "http://127.0.0.1:${host_port}/"
