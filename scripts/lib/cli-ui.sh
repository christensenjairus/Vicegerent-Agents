#!/usr/bin/env bash
# Shared terminal presentation helpers for vicegerent command-line scripts.

if [[ -t 1 && -z "${NO_COLOR+x}" ]]; then
  UI_BOLD=$'\033[1m'
  UI_CYAN=$'\033[0;36m'
  UI_GREEN=$'\033[0;32m'
  UI_YELLOW=$'\033[0;33m'
  UI_RED=$'\033[0;31m'
  UI_DIM=$'\033[2m'
  UI_RESET=$'\033[0m'
else
  UI_BOLD=""
  UI_CYAN=""
  UI_GREEN=""
  UI_YELLOW=""
  UI_RED=""
  UI_DIM=""
  UI_RESET=""
fi

# B/N: kept for inline styling inside heredoc usage text (only vicegerent), where the line-based ui_* helpers do not apply.
# shellcheck disable=SC2034 # These globals are consumed by sourcing scripts.
B="$UI_BOLD"
# shellcheck disable=SC2034
N="$UI_RESET"

ui_header() {
  local title="$1"
  shift
  printf '%s%s%s' "$UI_BOLD$UI_CYAN" "$title" "$UI_RESET"
  [[ $# -eq 0 ]] || printf '  %s%s%s' "$UI_DIM" "$*" "$UI_RESET"
  printf '\n'
}

ui_section() {
  printf '\n%s%s%s\n' "$UI_BOLD" "$*" "$UI_RESET"
}

ui_info() {
  printf '%s•%s %s\n' "$UI_CYAN" "$UI_RESET" "$*"
}

ui_success() {
  printf '%s✓%s %s\n' "$UI_GREEN" "$UI_RESET" "$*"
}

ui_warn() {
  printf '%s!%s %s\n' "$UI_YELLOW" "$UI_RESET" "$*" >&2
}

ui_error() {
  printf '%sERROR:%s %s\n' "$UI_RED$UI_BOLD" "$UI_RESET" "$*" >&2
}

ui_key_value() {
  local key="$1"
  shift
  printf '  %s%-16s%s %s\n' "$UI_DIM" "$key" "$UI_RESET" "$*"
}

ui_command() {
  local command="$1"
  shift
  printf '  %s$%s %s' "$UI_CYAN" "$UI_RESET" "$command"
  [[ $# -eq 0 ]] || printf '  %s# %s%s' "$UI_DIM" "$*" "$UI_RESET"
  printf '\n'
}
