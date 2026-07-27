#!/usr/bin/env bash

# Repair old Kind node images whose /kind mode blocks OCI hooks in pod user namespaces.
ensure_kind_userns_hook_access() {
  local node="$1" kind_mode

  if ! kind_mode="$(docker exec "$node" stat -c %a /kind)"; then
    ui_error "Cannot inspect /kind permissions in node ${node}."
    return 1
  fi
  [[ "$kind_mode" == 755 ]] && return 0

  # Upstream Kind PR #4179 made this the node-image default for hostUsers:false pods.
  ui_info "Repairing Kind user-namespace OCI hook access on ${node}…"
  if ! docker exec "$node" chmod 0755 /kind >/dev/null; then
    ui_error "Cannot set /kind permissions in node ${node}."
    return 1
  fi
  local repaired_mode
  if ! repaired_mode="$(docker exec "$node" stat -c %a /kind)"; then
    ui_error "Cannot verify /kind permissions in node ${node}."
    return 1
  fi
  if [[ "$repaired_mode" != 755 ]]; then
    ui_error "Node ${node} still does not expose /kind to user-namespace OCI hooks."
    return 1
  fi
  return 0
}
