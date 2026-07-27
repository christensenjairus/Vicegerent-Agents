#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/kind-userns.sh
source "$REPO_ROOT/scripts/lib/kind-userns.sh"

mode=700
chmod_calls=0
fail_initial_stat=0
fail_verify_stat=0
fail_chmod=0
preserve_mode=0
ui_info() { :; }
ui_error() { printf '%s\n' "$*" >&2; }
docker() {
  [[ "$1" == exec ]]
  shift
  [[ "$1" == vicegerent-control-plane ]]
  shift
  case "$1" in
    stat)
      [[ "$2" == -c && "$3" == %a && "$4" == /kind ]]
      if [[ "$chmod_calls" -eq 0 && "$fail_initial_stat" -eq 1 ]]; then
        return 1
      fi
      if [[ "$chmod_calls" -gt 0 && "$fail_verify_stat" -eq 1 ]]; then
        return 1
      fi
      printf '%s\n' "$mode"
      ;;
    chmod)
      [[ "$2" == 0755 && "$3" == /kind ]]
      [[ "$fail_chmod" -eq 0 ]] || return 1
      [[ "$preserve_mode" -eq 1 ]] || mode=755
      chmod_calls=$((chmod_calls + 1))
      ;;
    *)
      printf 'unexpected docker exec command: %s\n' "$*" >&2
      return 1
      ;;
  esac
}
reset_fixture() {
  mode=700
  chmod_calls=0
  fail_initial_stat=0
  fail_verify_stat=0
  fail_chmod=0
  preserve_mode=0
}
expect_failure() {
  if ensure_kind_userns_hook_access vicegerent-control-plane 2>/dev/null; then
    printf 'expected Kind hook repair to fail\n' >&2
    exit 1
  fi
}

ensure_kind_userns_hook_access vicegerent-control-plane
[[ "$mode" == 755 ]]
[[ "$chmod_calls" -eq 1 ]]
ensure_kind_userns_hook_access vicegerent-control-plane
[[ "$chmod_calls" -eq 1 ]]

reset_fixture
fail_initial_stat=1
expect_failure

reset_fixture
fail_chmod=1
expect_failure

reset_fixture
fail_verify_stat=1
expect_failure

reset_fixture
preserve_mode=1
expect_failure

printf 'OK - Kind user-namespace OCI hook repair is idempotent and fails closed\n'
