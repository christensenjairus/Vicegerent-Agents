#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"

cat > "$WORK/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
printf 'kubectl' >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\n' >> "$TEST_LOG"
while [[ $# -gt 0 && "$1" != -- ]]; do shift; done
[[ $# -gt 0 ]] || exit 0
shift
"$@"
EOF

cat > "$WORK/bin/tmux" <<'EOF'
#!/usr/bin/env bash
printf 'tmux' >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\n' >> "$TEST_LOG"
[[ "${1:-}" == -u ]] && shift
case "${1:-}" in
  list-sessions)
    [[ -n "${TEST_TMUX_SESSIONS:-}" ]] || exit 1
    printf '%s\n' "$TEST_TMUX_SESSIONS"
    ;;
  list-panes)
    [[ -n "${TEST_TMUX_PANES:-}" ]] || exit 1
    printf '%s\n' "$TEST_TMUX_PANES"
    ;;
  has-session)
    target=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == -t ]]; then
        target="$2"
        break
      fi
      shift
    done
    while IFS=$'\t' read -r session _; do
      [[ "$session" == "$target" ]] && exit 0
    done <<< "${TEST_TMUX_SESSIONS:-}"
    exit 1
    ;;
esac
EOF

cat > "$WORK/bin/fzf" <<'EOF'
#!/usr/bin/env bash
printf 'fzf' >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\n' >> "$TEST_LOG"
mapfile -t selections
selection="${selections[0]:-}"
match="${TEST_FZF_MATCH:-}"
prompt=""
expect=""
query=""
print_query=false
start_position=1
for arg in "$@"; do
  [[ "$arg" == --prompt=* ]] && prompt="${arg#--prompt=}"
  [[ "$arg" == --expect=* ]] && expect="${arg#--expect=}"
  [[ "$arg" == --query=* ]] && query="${arg#--query=}"
  [[ "$arg" == --print-query ]] && print_query=true
  if [[ "$arg" =~ ^--bind=start:pos\(([0-9]+)\)$ ]]; then
    start_position="${BASH_REMATCH[1]}"
  fi
done
(( start_position <= ${#selections[@]} )) && selection="${selections[start_position - 1]}"
case "$prompt" in
  'Session> ')
    key="${TEST_FZF_SESSION_KEY:-}"
    if (( $(grep -c '<--prompt=Session> >' "$TEST_LOG") > 1 )); then
      key="${TEST_FZF_SESSION_KEY_AFTER:-$key}"
      match="${TEST_FZF_SESSION_MATCH_AFTER:-$match}"
    fi
    ;;
  'Repository> ')
    key="${TEST_FZF_REPOSITORY_KEY:-}"
    if (( $(grep -c '<--prompt=Repository> >' "$TEST_LOG") > 1 )); then
      key="${TEST_FZF_REPOSITORY_KEY_AFTER:-$key}"
      match="${TEST_FZF_REPOSITORY_MATCH_AFTER:-$match}"
    fi
    ;;
  'Worktree> ')
    key="${TEST_FZF_WORKTREE_KEY:-}"
    match="${TEST_FZF_WORKTREE_MATCH:-$match}"
    if (( $(grep -c '<--prompt=Worktree> >' "$TEST_LOG") > 1 )); then
      key="${TEST_FZF_WORKTREE_KEY_AFTER:-$key}"
      match="${TEST_FZF_WORKTREE_MATCH_AFTER:-$match}"
    fi
    ;;
  'Prune worktree> ')
    key="${TEST_FZF_PRUNE_KEY:-}"
    match="${TEST_FZF_PRUNE_MATCH:-$match}"
    ;;
  'Worktree name: ')
    key="${TEST_FZF_BRANCH_KEY:-}"
    query="${TEST_FZF_BRANCH_QUERY:-$query}"
    if (( $(grep -c '<--prompt=Worktree name: >' "$TEST_LOG") > 1 )); then
      key="${TEST_FZF_BRANCH_KEY_AFTER:-$key}"
      query="${TEST_FZF_BRANCH_QUERY_AFTER:-$query}"
    fi
    ;;

  'Confirm prune> ')
    key="${TEST_FZF_CONFIRM_KEY:-}"
    match="${TEST_FZF_CONFIRM_MATCH:-$match}"
    ;;
  'Confirm force prune> ')
    key="${TEST_FZF_FORCE_CONFIRM_KEY:-}"
    match="${TEST_FZF_FORCE_CONFIRM_MATCH:-$match}"
    ;;
  'Existing session> ')
    key="${TEST_FZF_EXISTING_SESSION_KEY:-}"
    match="${TEST_FZF_EXISTING_SESSION_MATCH:-$match}"
    ;;
esac
printf 'fzf-first <%s> <%s>\n' "$prompt" "${selections[0]:-}" >> "$TEST_LOG"
for candidate in "${selections[@]}"; do
  [[ "$prompt" == 'Worktree name: ' ]] && candidate="${candidate#*$'\t'}"
  printf 'fzf-input <%s>\n' "$candidate" >> "$TEST_LOG"
done
if [[ -n "$match" ]]; then
  for candidate in "${selections[@]}"; do
    if [[ "$candidate" == *"$match"* ]]; then
      selection="$candidate"
      break
    fi
  done
fi
if [[ "${key:-}" == ctrl-c ]]; then
  exit 130
elif [[ "$print_query" == true && -n "${key:-}" && ",$expect," == *",$key,"* ]]; then
  printf '%s\n%s\n' "$query" "$key"
elif [[ "$print_query" == true ]]; then
  printf '%s\n\n%s\n' "$query" "$selection"
elif [[ -n "${key:-}" && ",$expect," == *",$key,"* ]]; then
  printf '%s\n%s\n' "$key" "$selection"
elif [[ -n "$expect" ]]; then
  printf '\n%s\n' "$selection"
else
  printf '%s\n' "$selection"
fi
EOF
chmod +x "$WORK/bin/kubectl" "$WORK/bin/tmux" "$WORK/bin/fzf"

pass=0
fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; fail=$((fail + 1)); }

assert_log_has() {
  local name="$1" expected="$2"
  if grep -Fq "$expected" "$TEST_LOG"; then
    ok "$name"
  else
    no "$name" "missing '$expected' in: $(tr '\n' ' ' < "$TEST_LOG")"
  fi
}

capture_tty() {
  local input="$1"
  shift
  python3 - "$input" "$@" <<'PY'
import errno
import os
import pty
import sys

input_text = sys.argv[1]
command = sys.argv[2:]
pid, fd = pty.fork()
if pid == 0:
    os.execvpe(command[0], command, os.environ)

os.write(fd, input_text.encode())
chunks = []
while True:
    try:
        chunk = os.read(fd, 4096)
    except OSError as error:
        if error.errno == errno.EIO:
            break
        raise
    if not chunk:
        break
    chunks.append(chunk)

_, status = os.waitpid(pid, 0)
sys.stdout.buffer.write(b"".join(chunks))
sys.exit(os.waitstatus_to_exitcode(status))
PY
}

export PATH="$WORK/bin:$PATH"
export TEST_LOG="$WORK/calls.log"
export VICEGERENT_WORKSPACE_ROOT="$WORK/workspace"
mkdir -p "$VICEGERENT_WORKSPACE_ROOT"
: > "$TEST_LOG"
expected_tmux_prefix="tmux <-u> <set-option> <-g> <default-shell> </bin/bash> <;>"
expected_tmux_prefix+=" <set-option> <-g> <default-command> </bin/bash> <;>"
expected_tmux_prefix+=" <set-option> <-g> <default-terminal> <tmux-256color> <;>"
expected_tmux_prefix+=" <set-option> <-g> <history-limit> <100000> <;>"
expected_tmux_prefix+=" <set-option> <-g> <focus-events> <on> <;>"
expected_tmux_prefix+=" <set-option> <-g> <status-style> <bg=colour234,fg=colour250> <;>"
expected_tmux_prefix+=" <set-option> <-g> <status-left-length> <60> <;>"
expected_tmux_prefix+=" <set-option> <-g> <status-right-length> <24> <;>"
expected_tmux_prefix+=" <set-option> <-g> <status-left> <#[bg=colour24,fg=white,bold] #S #[default]> <;>"
expected_tmux_prefix+=" <set-option> <-g> <status-right> <#[fg=colour245] %H:%M | %d %b > <;>"
expected_tmux_prefix+=" <set-option> <-g> <window-status-format> <#[fg=colour245] #I:#W > <;>"
expected_tmux_prefix+=" <set-option> <-g> <window-status-current-format> <#[bg=colour31,fg=white,bold] #I:#W#{?window_zoomed_flag, Z,} #[default]> <;>"
expected_tmux_prefix+=" <set-option> <-g> <mouse> <on> <;>"

: > "$TEST_LOG"
TERM=xterm-256color capture_tty '' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if grep '^fzf-input ' "$TEST_LOG" | grep -Fq $'\033'; then
  ok "colors interactive repository selection"
else
  no "colors interactive repository selection" "missing ANSI styling in fuzzy finder input"
fi

if grep '^fzf-input ' "$TEST_LOG" | grep -Fq $'\033[0;36m/workspace'; then
  ok "distinguishes the no-repository option with cyan"
else
  no "distinguishes the no-repository option with cyan" "missing cyan /workspace option"
fi

: > "$TEST_LOG"
NO_COLOR=1 TERM=xterm-256color capture_tty '' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if grep '^fzf-input ' "$TEST_LOG" | grep -Fq $'\033'; then
  no "honors NO_COLOR for interactive selection" "unexpected ANSI styling"
else
  ok "honors NO_COLOR for interactive selection"
fi

printf 'w\n' | "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "selects repositories with fuzzy finding" "fzf <--ansi> <--layout=reverse> <--border> <--height=100%>"
assert_log_has "creates main tmux session with usable terminal defaults" "$expected_tmux_prefix <new-session> <-s> <main> <-c> <$VICEGERENT_WORKSPACE_ROOT>"

repo="$VICEGERENT_WORKSPACE_ROOT/example"
mkdir -p "$repo"
git -C "$repo" init -q
git -C "$repo" config user.name test
git -C "$repo" config user.email test@example.invalid
printf 'seed\n' > "$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -qm initial
git -C "$repo" branch -m vicegerent
repo_branch="$(git -C "$repo" branch --show-current)"
origin="$WORK/example-origin.git"
git init --bare -q "$origin"
git -C "$origin" symbolic-ref HEAD "refs/heads/$repo_branch"
git -C "$repo" remote add origin "$origin"
/usr/bin/git -C "$origin" fetch -q "$repo" "$repo_branch:refs/heads/$repo_branch"
fresh_clone="$WORK/example-fresh"
git clone -q "$origin" "$fresh_clone"
git -C "$fresh_clone" config user.name test
git -C "$fresh_clone" config user.email test@example.invalid
printf 'fresh\n' >> "$fresh_clone/README.md"
git -C "$fresh_clone" commit -qam fresh
/usr/bin/git -C "$origin" fetch -q "$fresh_clone" "$repo_branch:refs/heads/$repo_branch"
fresh_head="$(git -C "$fresh_clone" rev-parse HEAD)"

: > "$TEST_LOG"
TERM=xterm-256color TEST_TMUX_SESSIONS="solo"$'\t'"$repo"$'\t1 windows\tdetached\tcreated today' \
  capture_tty '' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if grep '^fzf-input <session:' "$TEST_LOG" | grep -Fq $'\033[0;32msolo' \
  && grep '^fzf-input <new' "$TEST_LOG" | grep -Fq $'\033[0;36mcreate a new session'; then
  ok "colors sessions separately from session actions"
else
  no "colors sessions separately from session actions" "session items and actions did not use distinct colors"
fi

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=ctrl-n TEST_FZF_REPOSITORY_KEY=ctrl-w \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "offers session actions before repositories when no sessions exist" "<--prompt=Session> >"
assert_log_has "creates the derived workspace session after selecting new" "$expected_tmux_prefix <new-session> <-s> <main> <-c> <$VICEGERENT_WORKSPACE_ROOT>"

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=ctrl-s "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "offers a plain shell from the empty session selector" "<--prompt=Session> >"
if grep -Fq '<--prompt=Repository> >' "$TEST_LOG" || grep -Fq '<new-session>' "$TEST_LOG"; then
  no "plain shell bypasses directory and tmux selection" "unexpected selector or tmux call: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "plain shell bypasses directory and tmux selection"
fi

: > "$TEST_LOG"
TERM=xterm-256color capture_tty '' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if grep '^fzf-input <repo:' "$TEST_LOG" | grep -Fq $'\033[0;32mexample' \
  && grep '^fzf-input <new' "$TEST_LOG" | grep -Fq $'\033[0;36mcreate a new worktree'; then
  ok "colors repository and worktree actions separately from list items"
else
  no "colors repository and worktree actions separately from list items" "normal items and special actions did not use distinct colors"
fi

: > "$TEST_LOG"
TEST_FZF_REPOSITORY_KEY=ctrl-w "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "shows the repository action first" "fzf-first <Repository> > <workspace"
assert_log_has "keeps the repository action visible without changing the default" "<--height=100%> <--bind=start:pos(2)>"
assert_log_has "binds the repository shortcuts" "<--expect=esc,ctrl-w> <--prompt=Repository> >"
assert_log_has "repository shortcut selects the workspace" "$expected_tmux_prefix <new-session> <-s> <main> <-c> <$VICEGERENT_WORKSPACE_ROOT>"

: > "$TEST_LOG"
creation_output="$(TEST_FZF_WORKTREE_KEY=ctrl-n TEST_FZF_BRANCH_QUERY=shortcut-branch \
  "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"
assert_log_has "shows the worktree actions first" "fzf-first <Worktree> > <new"
assert_log_has "keeps the worktree actions visible without changing the default" "<--height=100%> <--bind=start:pos(4)>"
assert_log_has "binds the worktree shortcuts" "<--expect=esc,ctrl-n,ctrl-p,ctrl-w> <--prompt=Worktree> >"
assert_log_has "matches the selector layout without an input banner" "fzf <--ansi> <--phony> <--print-query> <--query=> <--border> <--height=100%> <--layout=reverse> <--delimiter="
assert_log_has "uses the selector-style worktree-name prompt" "<--with-nth=2..> <--pointer=> <--expect=esc> <--prompt=Worktree name: >"
if grep -Fq 'fzf-input <input>' "$TEST_LOG"; then
  no "does not show a fake input candidate" "unexpected candidate: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "does not show a fake input candidate"
fi
shortcut_worktree="$repo/.worktrees/shortcut-branch"
if [[ -d "$shortcut_worktree" ]] \
  && [[ "$(git -C "$shortcut_worktree" rev-parse HEAD)" == "$fresh_head" ]] \
  && [[ "$(git -C "$shortcut_worktree" branch --show-current)" == shortcut-branch ]] \
  && [[ "$(git -C "$repo" rev-parse HEAD)" != "$fresh_head" ]]; then
  ok "creates a named branch from the freshly fetched default branch"
else
  no "creates a named branch from the freshly fetched default branch" "worktree branch did not use $fresh_head while leaving the primary checkout stale"
fi
if [[ "$(< "$TEST_LOG")" == *"<display-message> <-d> <5000> <Branch 'shortcut-branch' is up to date with origin/vicegerent.>"* \
  && "$creation_output" != *"Branch 'shortcut-branch' is up to date"* ]]; then
  ok "shows branch freshness inside tmux"
else
  no "shows branch freshness inside tmux" "output=$creation_output log=$(tr '\n' ' ' < "$TEST_LOG")"
fi
if [[ "$creation_output" != *"do not pull"* \
  && "$creation_output" != *"Publish it with"* \
  && "$(< "$TEST_LOG")" != *"do not pull"* \
  && "$(< "$TEST_LOG")" != *"Publish it with"* ]]; then
  ok "reports branch status without Git instructions"
else
  no "reports branch status without Git instructions" "output=$creation_output log=$(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
if invalid_name_output="$(TEST_FZF_WORKTREE_KEY=ctrl-n TEST_FZF_BRANCH_QUERY='../invalid' \
  "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"; then
  no "fails invalid worktree names outside the TUI" "command unexpectedly succeeded"
elif [[ "$invalid_name_output" == *"Invalid worktree name: '../invalid'"* ]] \
  && [[ ! -e "$repo/.worktrees/valid-branch" ]]; then
  ok "fails invalid worktree names outside the TUI"
else
  no "fails invalid worktree names outside the TUI" "$invalid_name_output"
fi

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=ctrl-n TEST_FZF_REPOSITORY_KEY=ctrl-w \
  TEST_TMUX_SESSIONS="solo"$'\t'"$repo"$'\t1 windows\tdetached\tcreated today' \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "shows the session actions first" "fzf-first <Session> > <new"
assert_log_has "keeps the session actions visible without changing the default" "<--height=100%> <--bind=start:pos(3)>"
assert_log_has "binds the session shortcuts" "<--expect=esc,ctrl-n,ctrl-s> <--prompt=Session> >"
if grep -Fq '<--header=Enter: attach or create | Ctrl-N: new | Ctrl-S: shell | Esc:' "$TEST_LOG"; then
  no "does not advertise escape in the top-level session selector" "unexpected escape hint: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "does not advertise escape in the top-level session selector"
fi
assert_log_has "creates a derived session with the session shortcut" "$expected_tmux_prefix <new-session> <-s> <main> <-c> <$VICEGERENT_WORKSPACE_ROOT>"
if grep -Fq '<--prompt=Session name> >' "$TEST_LOG"; then
  no "does not offer custom tmux session names" "unexpected session-name input: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "does not offer custom tmux session names"
fi

: > "$TEST_LOG"
TEST_FZF_WORKTREE_KEY=ctrl-n TEST_FZF_WORKTREE_KEY_AFTER=ctrl-w TEST_FZF_BRANCH_KEY=esc \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Worktree name: >' "$TEST_LOG")" == 1 ]] \
  && [[ "$(grep -c '<--prompt=Worktree> >' "$TEST_LOG")" == 2 ]]; then
  ok "escape returns from worktree naming to worktrees"
else
  no "escape returns from worktree naming to worktrees" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_REPOSITORY_KEY=esc TEST_FZF_REPOSITORY_KEY_AFTER=ctrl-w "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Repository> >' "$TEST_LOG")" == 2 ]]; then
  ok "escape stays in the top-level repository selector"
else
  no "escape stays in the top-level repository selector" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=esc TEST_FZF_SESSION_KEY_AFTER=enter TEST_FZF_SESSION_MATCH_AFTER=solo \
  TEST_TMUX_SESSIONS="solo"$'\t'"$repo"$'\t1 windows\tdetached\tcreated today' \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Session> >' "$TEST_LOG")" == 2 ]] \
  && grep -Fq "$expected_tmux_prefix <attach-session> <-t> <solo>" "$TEST_LOG"; then
  ok "escape stays in the top-level session selector"
else
  no "escape stays in the top-level session selector" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_WORKTREE_KEY=esc TEST_FZF_REPOSITORY_KEY_AFTER=ctrl-w "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Repository> >' "$TEST_LOG")" == 2 ]] \
  && [[ "$(grep -c '<--prompt=Worktree> >' "$TEST_LOG")" == 1 ]]; then
  ok "escape returns from worktree selection to repositories"
else
  no "escape returns from worktree selection to repositories" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=ctrl-n TEST_FZF_SESSION_KEY_AFTER=enter TEST_FZF_SESSION_MATCH_AFTER=solo \
  TEST_FZF_REPOSITORY_KEY=esc \
  TEST_TMUX_SESSIONS="solo"$'\t'"$repo"$'\t1 windows\tdetached\tcreated today' \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Session> >' "$TEST_LOG")" == 2 ]] \
  && grep -Fq "$expected_tmux_prefix <attach-session> <-t> <solo>" "$TEST_LOG"; then
  ok "escape returns from repository selection to sessions"
else
  no "escape returns from repository selection to sessions" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_WORKTREE_KEY=ctrl-p TEST_FZF_WORKTREE_KEY_AFTER=ctrl-w TEST_FZF_PRUNE_KEY=esc \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Prune worktree> >' "$TEST_LOG")" == 1 ]] \
  && [[ "$(grep -c '<--prompt=Worktree> >' "$TEST_LOG")" == 2 ]]; then
  ok "escape returns from prune selection to worktrees"
else
  no "escape returns from prune selection to worktrees" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
if TEST_FZF_REPOSITORY_KEY=ctrl-c "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1; then
  no "control-c quits selection" "command unexpectedly succeeded"
else
  ok "control-c quits selection"
fi

help_output="$("$REPO_ROOT/vicegerent" ssh --help)"
if [[ "$help_output" == *"Ctrl-P opens the"* ]] \
  && [[ "$help_output" == *"prune selector"* ]] \
  && [[ "$help_output" != *'p<number>'* ]]; then
  ok "documents the current prune selector shortcut"
else
  no "documents the current prune selector shortcut" "$help_output"
fi

: > "$TEST_LOG"
if terminal_error_output="$(TERM=xterm-256color TEST_FZF_SESSION_KEY=ctrl-n TEST_FZF_REPOSITORY_MATCH=example \
  TEST_FZF_WORKTREE_KEY=ctrl-n TEST_FZF_BRANCH_QUERY=. capture_tty '' "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"; then
  no "preserves interactive error output after restoring the terminal" "command unexpectedly succeeded"
elif [[ "$terminal_error_output" == *"Invalid worktree name: '.'"* ]] \
  && [[ "$terminal_error_output" != *$'\033c'* ]]; then
  ok "preserves interactive error output after restoring the terminal"
else
  no "preserves interactive error output after restoring the terminal" "$terminal_error_output"
fi

upstream="$WORK/example-upstream.git"
git clone --bare -q "$origin" "$upstream"
git -C "$upstream" symbolic-ref HEAD "refs/heads/$repo_branch"
git -C "$repo" remote add upstream "$upstream"
upstream_clone="$WORK/example-upstream-fresh"
git clone -q "$upstream" "$upstream_clone"
git -C "$upstream_clone" config user.name test
git -C "$upstream_clone" config user.email test@example.invalid
printf 'upstream fresh\n' >> "$upstream_clone/README.md"
git -C "$upstream_clone" commit -qam 'upstream fresh'
/usr/bin/git -C "$upstream" fetch -q "$upstream_clone" "$repo_branch:refs/heads/$repo_branch"
upstream_head="$(git -C "$upstream_clone" rev-parse HEAD)"

: > "$TEST_LOG"
TEST_FZF_WORKTREE_MATCH='create a new worktree' TEST_FZF_BRANCH_QUERY=feature/test \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
worktree="$repo/.worktrees/feature/test"
if [[ -d "$worktree" ]] \
  && [[ "$(git -C "$worktree" branch --show-current)" == feature/test ]] \
  && [[ "$(git -C "$worktree" rev-parse HEAD)" == "$upstream_head" ]]; then
  ok "creates the requested branch and worktree from freshly fetched upstream"
else
  no "creates the requested branch and worktree from freshly fetched upstream" "missing feature/test branch at $upstream_head"
fi
assert_log_has "starts the worktree session with usable terminal defaults" "$expected_tmux_prefix <new-session> <-s> <example-feature-test> <-c> <$worktree>"

: > "$TEST_LOG"
if existing_branch_output="$(TEST_FZF_WORKTREE_KEY=ctrl-n TEST_FZF_BRANCH_QUERY=feature/test \
  "$REPO_ROOT/vicegerent" ssh alpha 2>&1)" \
  && grep -Fq "$expected_tmux_prefix <new-session> <-s> <example-feature-test> <-c> <$worktree>" "$TEST_LOG"; then
  ok "reuses the existing named worktree"
else
  no "reuses the existing named worktree" "$existing_branch_output"
fi

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=ctrl-n TEST_FZF_REPOSITORY_MATCH=example TEST_FZF_WORKTREE_MATCH=feature/test \
  TEST_TMUX_SESSIONS="example-feature-test"$'\t'"$worktree"$'\t1 windows\tdetached\tcreated today' \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if grep -Fq '<--prompt=Existing session> >' "$TEST_LOG" \
  && grep -Fq "$expected_tmux_prefix <attach-session> <-t> <example-feature-test>" "$TEST_LOG"; then
  ok "resumes an existing derived session after confirmation"
else
  no "resumes an existing derived session after confirmation" "unexpected command: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_SESSION_KEY=ctrl-n TEST_FZF_REPOSITORY_MATCH=example TEST_FZF_WORKTREE_MATCH=feature/test \
  TEST_FZF_WORKTREE_MATCH_AFTER='/workspace' TEST_FZF_EXISTING_SESSION_KEY=esc \
  TEST_TMUX_SESSIONS="example-feature-test"$'\t'"$worktree"$'\t1 windows\tdetached\tcreated today' \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Worktree> >' "$TEST_LOG")" == 2 ]] \
  && grep -Fq "$expected_tmux_prefix <new-session> <-s> <main> <-c> <$VICEGERENT_WORKSPACE_ROOT>" "$TEST_LOG"; then
  ok "returns to worktree selection when resuming is cancelled"
else
  no "returns to worktree selection when resuming is cancelled" "unexpected command: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
mkdir -p "$repo/.worktrees/taken"
if collision_output="$(TEST_FZF_WORKTREE_KEY=ctrl-n TEST_FZF_BRANCH_QUERY=taken \
  "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"; then
  no "fails unregistered worktree directories outside the TUI" "command unexpectedly succeeded"
elif [[ "$collision_output" == *"Worktree path already exists and is not a registered Git worktree: '$repo/.worktrees/taken'"* ]] \
  && [[ ! -e "$repo/.worktrees/available" ]]; then
  ok "fails unregistered worktree directories outside the TUI"
else
  no "fails unregistered worktree directories outside the TUI" "$collision_output"
fi

: > "$TEST_LOG"
TEST_FZF_MATCH='feature/test' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "selects worktrees with fuzzy finding" "<--prompt=Worktree> >"
assert_log_has "finds a worktree by name" "$expected_tmux_prefix <new-session> <-s> <example-feature-test> <-c> <$worktree>"

: > "$TEST_LOG"
TEST_TMUX_SESSIONS="solo"$'\t'"$VICEGERENT_WORKSPACE_ROOT"$'\t1 windows\tdetached\tcreated today' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "attaches after configuring usable terminal defaults" "$expected_tmux_prefix <attach-session> <-t> <solo>"
assert_log_has "selects running sessions with fuzzy finding" "fzf <--ansi> <--layout=reverse>"
if [[ "$(grep -c '^fzf ' "$TEST_LOG")" == 1 ]]; then
  ok "offers running sessions before repository selection"
else
  no "offers running sessions before repository selection" "expected one fuzzy selector"
fi

: > "$TEST_LOG"
TEST_FZF_MATCH=second TEST_TMUX_SESSIONS="first"$'\t'"$VICEGERENT_WORKSPACE_ROOT"$'\t1 windows\tdetached\tcreated today\n'"second"$'\t'"$repo"$'\t1 windows\tdetached\tcreated today' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "selects a session after configuring usable terminal defaults" "$expected_tmux_prefix <attach-session> <-t> <second>"

: > "$TEST_LOG"
TEST_FZF_MATCH="$worktree" TEST_TMUX_SESSIONS="first"$'\t'"$VICEGERENT_WORKSPACE_ROOT"$'\t1 windows\tdetached\tcreated today\n'"second"$'\t'"$worktree"$'\t1 windows\tdetached\tcreated today' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "finds a running session by its worktree path" "$expected_tmux_prefix <attach-session> <-t> <second>"

: > "$TEST_LOG"
TEST_FZF_MATCH="$worktree/src" TEST_TMUX_SESSIONS="work"$'\t'"$worktree/src"$'\t1 windows\tdetached\tcreated today' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "finds a session whose pane moved inside its worktree" "$expected_tmux_prefix <attach-session> <-t> <work>"

: > "$TEST_LOG"
prune_output="$(TEST_FZF_WORKTREE_KEY=ctrl-p TEST_FZF_WORKTREE_KEY_AFTER=ctrl-w TEST_FZF_PRUNE_MATCH='feature/test' TEST_FZF_CONFIRM_MATCH=cancel TERM=xterm-256color capture_tty '' "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"
if grep -Fq "<--prompt=Confirm prune> > <--header=Prune 'feature/test' at '$worktree'" "$TEST_LOG" && [[ -d "$worktree" ]]; then
  ok "confirms before pruning a selected worktree"
else
  no "confirms before pruning a selected worktree" "$prune_output"
fi

: > "$TEST_LOG"
TEST_FZF_WORKTREE_KEY=ctrl-p TEST_FZF_WORKTREE_KEY_AFTER=ctrl-w TEST_FZF_PRUNE_MATCH='feature/test' \
  TEST_FZF_CONFIRM_KEY=esc "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if [[ "$(grep -c '<--prompt=Confirm prune> >' "$TEST_LOG")" == 1 ]] \
  && [[ "$(grep -c '<--prompt=Worktree> >' "$TEST_LOG")" == 2 ]]; then
  ok "escape returns from prune confirmation to worktrees"
else
  no "escape returns from prune confirmation to worktrees" "unexpected selector history: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
TEST_FZF_WORKTREE_KEY=ctrl-p TEST_FZF_WORKTREE_KEY_AFTER=ctrl-w TEST_FZF_PRUNE_KEY=esc \
  "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
if ! grep -Fq $"fzf-input <prune:0\t" "$TEST_LOG" \
  && [[ "$(grep -c '^fzf-input <prune:' "$TEST_LOG")" == 2 ]]; then
  ok "omits the primary worktree from pruning"
else
  no "omits the primary worktree from pruning" "unexpected prune candidates: $(tr '\n' ' ' < "$TEST_LOG")"
fi

: > "$TEST_LOG"
active_output="$(TEST_FZF_WORKTREE_MATCH='prune a linked worktree' TEST_FZF_WORKTREE_MATCH_AFTER='/workspace' TEST_FZF_PRUNE_MATCH='feature/test' TEST_TMUX_PANES="$worktree/src" "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"
if [[ "$active_output" == *"A tmux pane is still using '$worktree'. Move or close it before pruning."* && -d "$worktree" ]]; then
  ok "refuses to prune a worktree used by a tmux pane"
else
  no "refuses to prune a worktree used by a tmux pane" "$active_output"
fi

: > "$TEST_LOG"
printf 'dirty\n' >> "$worktree/README.md"
dirty_output="$(TEST_FZF_WORKTREE_MATCH='prune a linked worktree' TEST_FZF_WORKTREE_MATCH_AFTER='/workspace' TEST_FZF_PRUNE_MATCH='feature/test' TEST_FZF_CONFIRM_MATCH='prune worktree' "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"
if grep -Fq '<--prompt=Confirm force prune> >' "$TEST_LOG" \
  && ! grep -Fq '<--prompt=Retry: >' "$TEST_LOG" && [[ -d "$worktree" ]]; then
  ok "asks before force-pruning a dirty worktree"
else
  no "asks before force-pruning a dirty worktree" "$dirty_output"
fi
git -C "$worktree" restore README.md

: > "$TEST_LOG"
prune_output="$(TEST_FZF_WORKTREE_MATCH='prune a linked worktree' TEST_FZF_WORKTREE_MATCH_AFTER='/workspace' TEST_FZF_PRUNE_MATCH='feature/test' TEST_FZF_CONFIRM_MATCH='prune worktree' "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"
if [[ ! -e "$worktree" ]] && git -C "$repo" show-ref --verify --quiet refs/heads/feature/test &&
   [[ "$prune_output" == *"Pruned worktree '$worktree'. The branch 'feature/test' was kept."* ]]; then
  ok "prunes the selected worktree and keeps its branch"
else
  no "prunes the selected worktree and keeps its branch" "$prune_output"
fi

force_worktree="$repo/.worktrees/force-test"
git -C "$repo" worktree add --detach "$force_worktree" >/dev/null 2>&1
printf 'dirty\n' >> "$force_worktree/README.md"
: > "$TEST_LOG"
force_prune_output="$(TEST_FZF_WORKTREE_MATCH='prune a linked worktree' TEST_FZF_WORKTREE_MATCH_AFTER=ctrl-w TEST_FZF_PRUNE_MATCH='force-test' TEST_FZF_CONFIRM_MATCH='prune worktree' TEST_FZF_FORCE_CONFIRM_MATCH='force remove worktree' "$REPO_ROOT/vicegerent" ssh alpha 2>&1)"
if [[ ! -e "$force_worktree" ]] \
  && grep -Fq '<--prompt=Confirm force prune> >' "$TEST_LOG"; then
  ok "force-prunes a dirty worktree after a second confirmation"
else
  no "force-prunes a dirty worktree after a second confirmation" "$force_prune_output"
fi

: > "$TEST_LOG"
TEST_FZF_MATCH='create a new session' TEST_FZF_WORKTREE_MATCH='no repository or worktree' TEST_TMUX_SESSIONS="first"$'\t'"$VICEGERENT_WORKSPACE_ROOT"$'\t1 windows\tdetached\tcreated today\n'"second"$'\t'"$repo"$'\t1 windows\tdetached\tcreated today' "$REPO_ROOT/vicegerent" ssh alpha >/dev/null 2>&1
assert_log_has "creates a derived session with usable terminal defaults" "$expected_tmux_prefix <new-session> <-s> <main> <-c> <$VICEGERENT_WORKSPACE_ROOT>"
if [[ "$(grep -c '^fzf ' "$TEST_LOG")" == 3 ]]; then
  ok "shows repositories only after requesting a new session"
else
  no "shows repositories only after requesting a new session" "unexpected fuzzy selector count"
fi

: > "$TEST_LOG"
if "$REPO_ROOT/vicegerent" ssh alpha --session review >/dev/null 2>&1; then
  no "rejects explicit tmux session names" "command unexpectedly succeeded"
elif [[ -s "$TEST_LOG" ]]; then
  no "rejects explicit tmux session names before kubectl" "unexpected command: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "rejects explicit tmux session names"
fi

: > "$TEST_LOG"
if "$REPO_ROOT/vicegerent" ssh alpha --new review >/dev/null 2>&1; then
  no "rejects explicit new tmux session names" "command unexpectedly succeeded"
elif [[ -s "$TEST_LOG" ]]; then
  no "rejects explicit new tmux session names before kubectl" "unexpected command: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "rejects explicit new tmux session names"
fi

: > "$TEST_LOG"
TEST_TMUX_SESSIONS='first' "$REPO_ROOT/vicegerent" ssh alpha --list >/dev/null 2>&1
assert_log_has "lists sessions with UTF-8 without attaching" "tmux <-u> <list-sessions>"
if grep -Eq '<(attach-session|new-session)>' "$TEST_LOG"; then
  no "list mode does not open a session" "unexpected attach/create in: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "list mode does not open a session"
fi
if grep '^kubectl ' "$TEST_LOG" | grep -Fq '<-it>'; then
  no "list mode does not allocate a TTY" "unexpected -it in kubectl call"
else
  ok "list mode does not allocate a TTY"
fi

: > "$TEST_LOG"
TEST_TMUX_SESSIONS='first' "$REPO_ROOT/vicegerent" ssh alpha --shell >/dev/null 2>&1
if grep -q '^tmux ' "$TEST_LOG"; then
  no "plain shell bypasses tmux" "unexpected tmux call in: $(tr '\n' ' ' < "$TEST_LOG")"
else
  ok "plain shell bypasses tmux"
fi

: > "$TEST_LOG"
if "$REPO_ROOT/vicegerent" ssh alpha --session >/dev/null 2>&1; then
  no "rejects a deprecated session option" "command unexpectedly succeeded"
elif [[ -s "$TEST_LOG" ]]; then
  no "rejects a deprecated session option before kubectl" "kubectl was called"
else
  ok "rejects a deprecated session option before kubectl"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
(( fail == 0 ))
