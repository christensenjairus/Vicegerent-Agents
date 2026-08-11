#!/usr/bin/env bash
# Shared parsing for the in-cluster webhook listener's Kubernetes Secret.
# Call collect_webhook_routes <values-file> [defaults-file]; it sets
# WEBHOOKS_ENABLED plus matching route labels and Secret keys without printing
# secret material. When a defaults file is given, agentDefaults is layered under
# each agent exactly as the webhook-listener chart does, so a route contributed
# by agentDefaults gets a signing Secret instead of deploying with none (a 503).

# valid_webhook_name rejects consecutive hyphens in addition to the base pattern:
# the installer derives the Secret key and env var by joining agent and route
# with "__" and mapping "-" to "_", so "a--b" would collide with that separator.
# Kept in lockstep with charts/webhook-listener, config.go, and
# validate-machine-values.py.
valid_webhook_name() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{0,62}$ && "$1" != *--* ]]
}

collect_webhook_routes() {
  local values_file="$1" defaults_file="${2:-}" merged rc
  if [[ -z "$defaults_file" ]]; then
    _collect_webhook_routes_from_file "$values_file"
    return
  fi
  merged="$(mktemp "${TMPDIR:-/tmp}/vicegerent-webhooks.XXXXXX")" || return 1
  # shellcheck disable=SC2016  # $defaults is a yq variable, not a shell one
  if ! yq ea '
      (select(fi==0).agentDefaults // {}) as $defaults
      | select(fi==1)
      | .agents = [((.agents // [])[]) | ($defaults * .)]
    ' "$defaults_file" "$values_file" > "$merged"; then
    rm -f "$merged"
    printf 'failed to layer agentDefaults from %s under agents in %s\n' "$defaults_file" "$values_file" >&2
    return 1
  fi
  _collect_webhook_routes_from_file "$merged" "$values_file"
  rc=$?
  rm -f "$merged"
  return "$rc"
}

# _collect_webhook_routes_from_file <read-file> [display-file]
# Reads the (already merged) config from read-file; error messages name
# display-file so operators see their real values path, not a temp file.
_collect_webhook_routes_from_file() {
  local values_file="$1" display_file="${2:-$1}" idx agent_name webhook_enabled route route_type route_enabled
  local active_routes
  # shellcheck disable=SC2034  # output consumed by the sourcing script
  WEBHOOKS_ENABLED=0
  WEBHOOK_ROUTE_LABELS=()
  WEBHOOK_SECRET_KEYS=()

  while IFS= read -r idx; do
    [[ -n "$idx" ]] || continue
    webhook_enabled="$(yq -r ".agents[$idx].webhooks.enabled" "$values_file")"
    [[ "$webhook_enabled" == "null" || "$webhook_enabled" == "false" ]] && continue
    if [[ "$webhook_enabled" != "true" ]]; then
      printf 'agents[%s].webhooks.enabled in %s must be a boolean\n' "$idx" "$display_file" >&2
      return 1
    fi
    # shellcheck disable=SC2034  # output consumed by the sourcing script
    WEBHOOKS_ENABLED=1
    active_routes=0
    agent_name="$(yq -r ".agents[$idx].name // \"\"" "$values_file")"
    if ! valid_webhook_name "$agent_name"; then
      printf 'agents[%s].name %q in %s must match ^[a-z][a-z0-9-]{0,62}$ with no consecutive hyphens\n' "$idx" "$agent_name" "$display_file" >&2
      return 1
    fi

    if [[ "$(yq -r ".agents[$idx].webhooks.routes // {} | type" "$values_file")" != "!!map" ]]; then
      printf 'agents[%s].webhooks.routes in %s must be a YAML map\n' "$idx" "$display_file" >&2
      return 1
    fi

    while IFS= read -r route; do
      [[ -n "$route" ]] || continue
      if ! valid_webhook_name "$route"; then
        printf 'webhook route name %q in %s must match ^[a-z][a-z0-9-]{0,62}$ with no consecutive hyphens\n' "$route" "$display_file" >&2
        return 1
      fi

      route_type="$(ROUTE_NAME="$route" yq -r ".agents[$idx].webhooks.routes[strenv(ROUTE_NAME)] | type" "$values_file")"
      if [[ "$route_type" != "!!map" ]]; then
        printf 'agents[%s].webhooks.routes.%s in %s must be a YAML map\n' "$idx" "$route" "$display_file" >&2
        return 1
      fi

      route_enabled="$(ROUTE_NAME="$route" yq -r ".agents[$idx].webhooks.routes[strenv(ROUTE_NAME)].enabled" "$values_file")"
      [[ "$route_enabled" == "null" ]] && route_enabled=true
      [[ "$route_enabled" == "false" ]] && continue
      [[ "$route_enabled" == "true" ]] || {
        printf 'agents[%s].webhooks.routes.%s.enabled in %s must be a boolean\n' "$idx" "$route" "$display_file" >&2
        return 1
      }
      active_routes=$((active_routes + 1))

      WEBHOOK_ROUTE_LABELS+=("$agent_name/$route")
      WEBHOOK_SECRET_KEYS+=("$(webhook_secret_key "$agent_name" "$route")")
    done < <(yq -r ".agents[$idx].webhooks.routes // {} | keys[]" "$values_file")

    if [[ "$active_routes" -eq 0 ]]; then
      printf 'agents[%s] enables webhooks in %s but has no enabled routes\n' "$idx" "$display_file" >&2
      return 1
    fi
  done < <(yq -r '.agents // [] | to_entries[].key' "$values_file")
}

webhook_secret_key() {
  printf '%s__%s' "$1" "$2"
}

webhook_secret_envvar() {
  printf 'WEBHOOK_SECRET_%s' "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
}

# Resolves machine values when the usual values.yaml is absent.
select_values_file() {
  local repo_root="$1" assume_yes="$2" values_file_explicit="$3" candidate choice
  local candidates=()

  [[ -f "$VALUES_FILE" ]] && return 0

  if [[ "$values_file_explicit" == "1" ]]; then
    printf 'machine values not found: %s\n' "$VALUES_FILE" >&2
    return 1
  fi

  if declare -F ui_warn >/dev/null 2>&1; then
    ui_warn "Machine values not found: $VALUES_FILE"
  else
    printf 'WARNING: Machine values not found: %s\n' "$VALUES_FILE" >&2
  fi

  for candidate in "$repo_root"/examples/*.yaml; do
    [[ -f "$candidate" ]] || continue
    candidates+=("$candidate")
  done

  case "${#candidates[@]}" in
    0) return 0 ;;
    1)
      VALUES_FILE="${candidates[0]}"
      printf 'Using machine values from %s.\n' "$VALUES_FILE"
      return 0
      ;;
  esac

  if [[ "$assume_yes" == "1" ]]; then
    printf 'multiple example profiles found; set VALUES_FILE explicitly for non-interactive setup\n' >&2
    return 1
  fi

  printf 'Machine values:\n'
  local index=1
  for candidate in "${candidates[@]}"; do
    printf '  %d) %s\n' "$index" "${candidate#"$repo_root"/}"
    index=$((index + 1))
  done
  read -r -p "Select profile [1-${#candidates[@]}]: " choice
  [[ "$choice" =~ ^[0-9]+$ && "$choice" -ge 1 && "$choice" -le "${#candidates[@]}" ]] || {
    printf 'invalid machine values profile selection\n' >&2
    return 1
  }
  VALUES_FILE="${candidates[$((choice - 1))]}"
  printf 'Using machine values from %s.\n' "$VALUES_FILE"
}
