#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=install/lib/webhooks.sh
source "$repo_root/scripts/install/lib/webhooks.sh"

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-webhook-secrets.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT INT TERM

write_values() {
  printf '%s\n' "$1" > "$tmpdir/values.yaml"
}

write_profile() {
  mkdir -p "$tmpdir/examples"
  printf '%s\n' 'agents: []' > "$tmpdir/examples/$1.yaml"
}

expect_invalid() {
  if collect_webhook_routes "$tmpdir/values.yaml" 2>/dev/null; then
    printf 'expected invalid webhook values:\n%s\n' "$(cat "$tmpdir/values.yaml")" >&2
    exit 1
  fi
}

write_values 'agents: [{name: plain}]'
collect_webhook_routes "$tmpdir/values.yaml"
[[ "$WEBHOOKS_ENABLED" == "0" && ${#WEBHOOK_SECRET_KEYS[@]} -eq 0 ]]

write_values 'agents:
  - name: first
    webhooks:
      enabled: true
      routes:
        incidents: {provider: pagerduty}
        disabled: {enabled: false}
  - name: second
    webhooks:
      enabled: true
      routes:
        pushes: {provider: github}
        deploys: {provider: gitlab}'
collect_webhook_routes "$tmpdir/values.yaml"
[[ "$WEBHOOKS_ENABLED" == "1" ]]
[[ "${WEBHOOK_ROUTE_LABELS[*]}" == "first/incidents second/pushes second/deploys" ]]
[[ "${WEBHOOK_SECRET_KEYS[*]}" == "first__incidents second__pushes second__deploys" ]]
[[ "$(webhook_secret_envvar first-agent__deploy-events)" == "WEBHOOK_SECRET_FIRST_AGENT__DEPLOY_EVENTS" ]]

VALUES_FILE="$tmpdir/values.yaml"
select_values_file "$tmpdir" 0 0
[[ "$VALUES_FILE" == "$tmpdir/values.yaml" ]]

VALUES_FILE="$tmpdir/missing.yaml"
select_values_file "$tmpdir" 0 0
[[ "$VALUES_FILE" == "$tmpdir/missing.yaml" ]]

VALUES_FILE="$tmpdir/missing.yaml"
if select_values_file "$tmpdir" 0 1; then
  printf 'expected explicit missing values file to fail\n' >&2
  exit 1
fi

write_profile personal
VALUES_FILE="$tmpdir/missing.yaml"
select_values_file "$tmpdir" 0 0
[[ "$VALUES_FILE" == "$tmpdir/examples/personal.yaml" ]]

write_profile work
VALUES_FILE="$tmpdir/missing.yaml"
ui_warn() { printf '<yellow>!<reset> %s\n' "$*" >&2; }
select_values_file "$tmpdir" 0 0 <<< '2' > "$tmpdir/selection-output" 2>&1
[[ "$VALUES_FILE" == "$tmpdir/examples/work.yaml" ]]
selection_output="$(cat "$tmpdir/selection-output")"
[[ "$selection_output" == *"<yellow>!<reset> Machine values not found: $tmpdir/missing.yaml"* ]]
warning_line="$(grep -n 'Machine values not found:' "$tmpdir/selection-output" | cut -d: -f1)"
menu_line="$(grep -n '^Machine values:' "$tmpdir/selection-output" | cut -d: -f1)"
[[ "$warning_line" -lt "$menu_line" ]]

VALUES_FILE="$tmpdir/missing.yaml"
if select_values_file "$tmpdir" 1 0; then
  printf 'expected non-interactive profile selection to fail\n' >&2
  exit 1
fi

write_values 'agents: [{name: test, webhooks: {enabled: true, routes: []}}]'
expect_invalid

write_values 'agents: [{name: test, webhooks: {enabled: yes, routes: {}}}]'
expect_invalid

write_values 'agents: [{name: test, webhooks: {enabled: true, routes: {incidents: nope}}}]'
expect_invalid

write_values 'agents: [{name: test, webhooks: {enabled: true, routes: {incidents: {enabled: maybe}}}}]'
expect_invalid

write_values 'agents: [{name: test, webhooks: {enabled: true, routes: {incidents: {enabled: false}}}}]'
expect_invalid

# Consecutive hyphens collide with the "__" Secret-key separator (an env var
# derived from x--y and the x/y separator both map to X__Y), so reject them in
# both agent and route names.
write_values 'agents: [{name: bad--agent, webhooks: {enabled: true, routes: {incidents: {provider: pagerduty}}}}]'
expect_invalid
write_values 'agents: [{name: good, webhooks: {enabled: true, routes: {bad--route: {provider: pagerduty}}}}]'
expect_invalid

# agentDefaults must be layered under each agent exactly as the webhook-listener
# chart does, so a route contributed by agentDefaults is discovered here and
# provisioned a signing Secret instead of deploying to a 503.
printf '%s\n' 'agentDefaults:
  webhooks:
    enabled: true
    routes:
      shared: {provider: github}' > "$tmpdir/defaults.yaml"
write_values 'agents:
  - name: inherits
  - name: adds
    webhooks:
      routes:
        own: {provider: gitlab}'
collect_webhook_routes "$tmpdir/values.yaml" "$tmpdir/defaults.yaml"
[[ "$WEBHOOKS_ENABLED" == "1" ]]
[[ "${WEBHOOK_ROUTE_LABELS[*]}" == "inherits/shared adds/shared adds/own" ]]
[[ "${WEBHOOK_SECRET_KEYS[*]}" == "inherits__shared adds__shared adds__own" ]]

# Without the defaults layer the same values file surfaces no routes, proving it
# is the merge -- not the raw values -- that reveals the agentDefaults route.
collect_webhook_routes "$tmpdir/values.yaml"
[[ "$WEBHOOKS_ENABLED" == "0" && ${#WEBHOOK_SECRET_KEYS[@]} -eq 0 ]]

printf 'Webhook Secret discovery tests passed.\n'
