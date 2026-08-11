#!/usr/bin/env bash

# provider_enabled <provider> <defaults-file> <values-file>
# Aborts (exit 1) if yq cannot evaluate the merged config, so a malformed values
# file surfaces as an error instead of being silently read as "provider disabled"
# and skipping that provider's API-key secret.
provider_enabled() {
  local provider="$1" defaults_file="$2" values_file="$3" enabled
  local files=("$defaults_file")
  [[ -f "$values_file" ]] && files+=("$values_file")
  if ! enabled="$(yq ea -r ". as \$item ireduce ({}; . * \$item) | .models.${provider}.enabled // false" "${files[@]}")"; then
    printf 'provider_enabled: yq failed to evaluate models.%s.enabled from %s\n' "$provider" "${files[*]}" >&2
    exit 1
  fi
  [[ "$enabled" == "true" ]]
}
