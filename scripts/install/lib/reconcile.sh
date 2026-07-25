#!/usr/bin/env bash
# Cross-release pruning of agents. Helm prunes objects *within* a release on
# upgrade, but a whole agent dropped from values.yaml is a release Helm never sees
# again — so after the agents stage we diff the desired agent set against the
# releases actually present in agent-sandbox and uninstall the orphans.
#
# Safe because agent-sandbox holds only per-agent releases: the controller lives
# in agent-sandbox-system, platform in agentgateway-system, egress-proxy/cerbos in
# their own namespaces. Only the agent chart installs into agent-sandbox.
reconcile_agents() {
  local want=() have=() w h keep
  while IFS= read -r w; do [[ -n "$w" ]] && want+=("$w"); done < <(yq '.agents[].name' "$VALUES_FILE")
  while IFS= read -r h; do [[ -n "$h" ]] && have+=("$h"); done < <(helmc list -n agent-sandbox -q)

  # ${arr[@]+…}: a fresh cluster has zero agent releases (have empty) and a
  # values.yaml may list none (want empty); an empty array is "unbound" under
  # bash-3.2/macOS set -u, so guard both expansions.
  for h in ${have[@]+"${have[@]}"}; do
    keep=0
    for w in ${want[@]+"${want[@]}"}; do [[ "$h" == "$w" ]] && keep=1 && break; done
    [[ "$keep" == 1 ]] && continue
    warn "agent release '${h}' is deployed but not in values.yaml"
    if confirm "Uninstall dropped agent release '${h}' from agent-sandbox."; then
      helmc uninstall "$h" -n agent-sandbox --wait || warn "uninstall of '${h}' reported an error"
    fi
  done
}
