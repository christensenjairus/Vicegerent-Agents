#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prompt_script="$repo_root/images/agent/bash-prompt.sh"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

pass=0
fail=0

ok() {
  printf 'ok   %s\n' "$1"
  pass=$((pass + 1))
}

no() {
  printf 'FAIL %s: %s\n' "$1" "$2" >&2
  fail=$((fail + 1))
}

assert_contains() {
  local name=$1 value=$2 expected=$3
  if [[ "$value" == *"$expected"* ]]; then
    ok "$name"
  else
    no "$name" "expected <$expected> in <$value>"
  fi
}

git init -q -b prompt-test "$tmp_dir/repo"
git -C "$tmp_dir/repo" config user.name test
git -C "$tmp_dir/repo" config user.email test@example.invalid
: > "$tmp_dir/repo/tracked"
git -C "$tmp_dir/repo" add tracked
git -C "$tmp_dir/repo" commit -qm initial

plain_output="$(NO_COLOR=1 TERM=xterm-256color bash --noprofile --norc -ic '
  cd "$1"
  source "$2"
  false
  __vicegerent_prompt_command
  printf "repo=%s\nbranch=%s\ndirty=%s\nstatus=%s\nps1=%q\n" \
    "$__vicegerent_prompt_repo" "$__vicegerent_prompt_branch" \
    "$__vicegerent_prompt_dirty" "$__vicegerent_prompt_status" "$PS1"
' bash "$tmp_dir/repo" "$prompt_script" 2>/dev/null)"
assert_contains "shows the repository name" "$plain_output" 'repo=repo'
assert_contains "shows the Git branch" "$plain_output" 'branch=prompt-test'
assert_contains "shows the previous exit status" "$plain_output" 'status=✗ 1'
assert_contains "uses the plain fallback prompt when NO_COLOR is set" "$plain_output" '> '

printf 'dirty\n' >> "$tmp_dir/repo/tracked"
dirty_output="$(NO_COLOR=1 TERM=xterm-256color bash --noprofile --norc -ic '
  cd "$1"
  source "$2"
  __vicegerent_prompt_command
  printf "dirty=%s\n" "$__vicegerent_prompt_dirty"
' bash "$tmp_dir/repo" "$prompt_script" 2>/dev/null)"
assert_contains "marks tracked Git changes" "$dirty_output" 'dirty=*'

# shellcheck disable=SC2016
color_output="$(env -u NO_COLOR TERM=xterm-256color bash --noprofile --norc -ic '
  cd "$1"
  source "$2"
  __vicegerent_prompt_command
  printf "%q\n" "$PS1"
' bash "$tmp_dir/repo" "$prompt_script" 2>/dev/null)"
assert_contains "uses ANSI colors in capable terminals" "$color_output" '\e[1;36m'
assert_contains "uses the styled continuation glyph" "$color_output" '❯'

noninteractive_output="$(bash -c 'source "$1"; printf "%s\n" "${PROMPT_COMMAND-unset}"' bash "$prompt_script")"
if [[ "$noninteractive_output" == unset ]]; then
  ok "does not alter non-interactive Bash"
else
  no "does not alter non-interactive Bash" "expected <unset>, got <$noninteractive_output>"
fi

if (( fail > 0 )); then
  printf '\n%d passed, %d failed\n' "$pass" "$fail" >&2
  exit 1
fi
printf '\n%d passed, 0 failed\n' "$pass"
