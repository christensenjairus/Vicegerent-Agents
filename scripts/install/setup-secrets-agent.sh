#!/usr/bin/env bash
# Idempotent secret setup for a single vicegerent agent, using Kubernetes Secrets
# directly (no 1Password). All material lives in the agent-sandbox namespace.
#
# Usage: setup-secrets-agent.sh <agent-name> [-y|--yes]
#
# Applies these Kubernetes Secrets in namespace agent-sandbox:
#   <name>-secrets                 password, signing-secret, public-key,
#                                  SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
#                                  SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (Slack optional)
#   <name>-ssh-key                 hermes_agent_ed25519  (ed25519 private key)
#
# Generated material (dashboard auth, SSH key) is generated once and
# reused on re-run; Slack values are taken from the environment or prompted for.
# Secrets are disposable/recreatable — keep your own copy of any Slack tokens.
#
# Flags:
#   -y, --yes     auto-approve every change (non-interactive)
#   -h, --help    show this help
#
# Env overrides: SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
#   SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/kube-context.sh
source "$SCRIPT_DIR/../lib/kube-context.sh"

NS=agent-sandbox

ASSUME_YES=0
AGENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) [[ -z "$AGENT" ]] && AGENT="$1" || { echo "unexpected argument: $1" >&2; exit 2; } ;;
  esac
  shift
done

[[ -n "$AGENT" ]] || { echo "usage: $0 <agent-name> [-y|--yes]" >&2; exit 2; }
AGENT="$(echo "$AGENT" | tr '[:upper:]' '[:lower:]')"

ITEM="${AGENT}-secrets"
ITEM_SSH="${AGENT}-ssh-key"  # pragma: allowlist secret

# Fixed key name: the sandbox mounts this Secret at /opt/hermes-ssh/<name> and
# GIT_SSH_COMMAND in the chart references hermes_agent_ed25519.
SSH_KEY_FILE="hermes_agent_ed25519"

# shellcheck source=../lib/cli-ui.sh
source "$SCRIPT_DIR/../lib/cli-ui.sh"

info()  { ui_info "$@"; }
step()  { ui_section "$@"; }
warn()  { ui_warn "$@"; }
die()   { ui_error "$@"; exit 1; }

confirm() {
  echo
  echo "${UI_YELLOW}${UI_BOLD}Change${UI_RESET}  $*"
  if [[ "$ASSUME_YES" == "1" ]]; then
    echo "  (auto-approved via --yes)"
    return 0
  fi
  local ans
  read -r -p "  Proceed? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }
ensure_ns() { kc create namespace "$1" --dry-run=client -o yaml | kc apply -f - >/dev/null; }
# secret_val <name> <key> — decoded value of a secret key (empty if absent).
secret_val() {
  local json b64
  json="$(kc -n "$NS" get secret "$1" -o json 2>/dev/null)" || return 0
  b64="$(printf '%s' "$json" | jq -r --arg k "$2" '.data[$k] // empty')"
  [[ -n "$b64" ]] && printf '%s' "$b64" | base64 -d
  return 0
}

# --- prerequisites ---------------------------------------------------------
for cmd in kubectl openssl ssh-keygen jq; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not on PATH"
done
require_kind_context

WORK="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-agent-setup.XXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

ui_header "Agent secrets" "agent: $AGENT  •  context: $KUBE_CONTEXT"
ensure_ns "$NS"

# --- SSH key ---------------------------------------------------------------
# ed25519 keypair (generate-once). Private key → <name>-ssh-key; public key is
# stored as the public-key field of <name>-secrets (assembled below).
step "SSH identity"
pubkey="$(secret_val "$ITEM" public-key || true)"
if [[ -n "$(secret_val "$ITEM_SSH" "$SSH_KEY_FILE")" ]]; then
  info "SSH key already present; reusing."
  [[ -n "$pubkey" ]] && { echo; ui_key_value "Public key" "$pubkey"; ui_info "Add this key to GitLab/GitHub if it is not already registered."; }
else
  if confirm "Generate a new ed25519 SSH key for agent '$AGENT' ($ITEM_SSH)."; then
    ssh-keygen -t ed25519 -C "${AGENT}-agent@vicegerent" -N "" -f "$WORK/$SSH_KEY_FILE" >/dev/null 2>&1
    kc -n "$NS" create secret generic "$ITEM_SSH" \
      --from-file="$SSH_KEY_FILE=$WORK/$SSH_KEY_FILE" \
      --dry-run=client -o yaml | kc apply -f - >/dev/null
    pubkey="$(cat "$WORK/$SSH_KEY_FILE.pub")"
    info "Stored SSH private key in $ITEM_SSH."
    echo
    ui_key_value "Public key" "$pubkey"
    ui_info "Next: add this key to your GitLab/GitHub deploy keys."
  else
    warn "SSH key generation skipped — git push/pull from the sandbox will not work until set."
  fi
fi

# --- agent secrets (dashboard auth + Slack + public key) -------------------
# Assembled and applied as a whole because `apply` replaces every key; existing
# generated values (password, signing-secret) and Slack fields are preserved.
step "Agent credentials"
password="$(secret_val "$ITEM" password || true)"
signing="$(secret_val "$ITEM" signing-secret || true)"
[[ -z "$password" ]] && { password="$(openssl rand -base64 24 | tr -d '\n')"; info "Generated dashboard password."; }
[[ -z "$signing" ]] && { signing="$(openssl rand -base64 32 | tr -d '\n')"; info "Generated dashboard signing-secret."; }

args=(--from-literal="password=$password" --from-literal="signing-secret=$signing")
[[ -n "$pubkey" ]] && args+=(--from-literal="public-key=$pubkey")

# Slack fields (optional). env override > existing value > interactive prompt.
echo
ui_info "Slack is optional. Create the app from slack-app-manifest.yaml, enable Socket Mode, then install it to the workspace."
for field in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL; do
  val="${!field:-}"
  [[ -z "$val" ]] && val="$(secret_val "$ITEM" "$field" || true)"
  if [[ -z "$val" && "$ASSUME_YES" != "1" ]]; then
    case "$field" in
      # The two tokens are credentials, so they are not echoed; the allowed-users
      # and home-channel values are not secret and are easier to check on screen.
      *_TOKEN) read -r -s -p "  $field (empty to skip): " val; echo ;;
      *)       read -r -p "  $field (empty to skip): " val ;;
    esac
  fi
  if [[ -n "$val" ]]; then
    args+=(--from-literal="$field=$val")
    info "$field set."
  fi
done

kc -n "$NS" create secret generic "$ITEM" "${args[@]}" --dry-run=client -o yaml | kc apply -f - >/dev/null
info "Applied $ITEM."

# --- verify ----------------------------------------------------------------
step "Verification"
missing=0
check() { if [[ -n "$(secret_val "$1" "$2")" ]]; then ui_success "$NS/$1 ($2)"; else ui_error "$NS/$1 ($2) is missing"; missing=1; fi; }
check_optional() { if [[ -n "$(secret_val "$1" "$2")" ]]; then ui_success "$NS/$1 ($2)"; else ui_info "$NS/$1 ($2) — optional, not set"; fi; }
check "$ITEM" password
check "$ITEM" signing-secret
check_optional "$ITEM" public-key
check_optional "$ITEM_SSH" "$SSH_KEY_FILE"
check_optional "$ITEM" SLACK_BOT_TOKEN
check_optional "$ITEM" SLACK_APP_TOKEN
check_optional "$ITEM" SLACK_ALLOWED_USERS
check_optional "$ITEM" SLACK_HOME_CHANNEL

echo
if [[ $missing -eq 0 ]]; then
  info "All required secret material for agent '$AGENT' is present."
else
  warn "Some required material is missing (see above). Re-run to complete it."
  exit 1
fi
