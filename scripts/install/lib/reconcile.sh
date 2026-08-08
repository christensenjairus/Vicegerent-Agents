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
  local want=() have=() candidates=() remaining=() residual=() failed=() w h r keep desired deployed observed
  if ! desired="$(yq '.agents[].name' "$VALUES_FILE")"; then
    warn "unable to read desired agent releases from values.yaml"
    return 1
  fi
  if ! deployed="$(helmc list -n agent-sandbox -q)"; then
    warn "unable to list deployed agent releases"
    return 1
  fi
  while IFS= read -r w; do [[ -n "$w" ]] && want+=("$w"); done <<<"$desired"
  while IFS= read -r h; do [[ -n "$h" ]] && have+=("$h"); done <<<"$deployed"

  # ${arr[@]+…}: a fresh cluster has zero agent releases (have empty) and a
  # values.yaml may list none (want empty); an empty array is "unbound" under
  # bash-3.2/macOS set -u, so guard both expansions.
  for h in ${have[@]+"${have[@]}"}; do
    keep=0
    for w in ${want[@]+"${want[@]}"}; do [[ "$h" == "$w" ]] && keep=1 && break; done
    [[ "$keep" == 1 ]] && continue
    candidates+=("$h")
    warn "agent release '${h}' is deployed but not in values.yaml"
    if confirm "Uninstall dropped agent release '${h}' from agent-sandbox."; then
      if ! helmc uninstall "$h" -n agent-sandbox --wait; then
        warn "uninstall of '${h}' reported an error"
        failed+=("$h")
      fi
    fi
  done

  if ! observed="$(helmc list -n agent-sandbox -q)"; then
    warn "unable to verify deployed agent releases after pruning"
    return 1
  fi
  while IFS= read -r r; do [[ -n "$r" ]] && remaining+=("$r"); done <<<"$observed"
  for h in ${candidates[@]+"${candidates[@]}"}; do
    for r in ${remaining[@]+"${remaining[@]}"}; do [[ "$h" == "$r" ]] && residual+=("$h") && break; done
  done
  # A failed Helm operation remains an error even if its release disappeared
  # before the verification list. Include its identifier in the failure report.
  for h in ${failed[@]+"${failed[@]}"}; do
    keep=0
    for r in ${residual[@]+"${residual[@]}"}; do [[ "$h" == "$r" ]] && keep=1 && break; done
    [[ "$keep" == 1 ]] || residual+=("$h")
  done
  if ((${#residual[@]})); then
    warn "agent release pruning did not converge; residual releases: ${residual[*]}"
    return 1
  fi
}
