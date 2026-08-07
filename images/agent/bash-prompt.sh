#!/usr/bin/env bash
# Vicegerent's interactive Bash prompt. This file is sourced from
# /etc/bash.bashrc so it affects interactive Bash (including tmux) only.
# Keep it dependency-free apart from Git, which is already in the agent image.

[[ $- == *i* ]] || return 0

# A short path retains enough worktree context without letting a deeply nested
# checkout consume the whole prompt. This only affects the \w prompt escape.
PROMPT_DIRTRIM=4

__vicegerent_prompt_last_pwd=''
__vicegerent_prompt_last_update=-1
__vicegerent_prompt_repo=''
__vicegerent_prompt_branch=''
__vicegerent_prompt_dirty=''
__vicegerent_prompt_git_context=''
__vicegerent_prompt_status=''

__vicegerent_prompt_update_git() {
  # Git status can be expensive in very large repositories. Recompute when the
  # directory changes and otherwise at most once a second; the dirty indicator
  # is therefore intentionally allowed to lag by less than one second.
  if [[ "$PWD" == "$__vicegerent_prompt_last_pwd" ]] \
    && (( SECONDS - __vicegerent_prompt_last_update < 1 )); then
    return
  fi

  __vicegerent_prompt_last_pwd=$PWD
  __vicegerent_prompt_last_update=$SECONDS
  __vicegerent_prompt_repo=''
  __vicegerent_prompt_branch=''
  __vicegerent_prompt_dirty=''
  __vicegerent_prompt_git_context=''

  local root
  root="$(command git rev-parse --show-toplevel 2>/dev/null)" || return 0
  __vicegerent_prompt_repo=${root##*/}
  __vicegerent_prompt_branch="$(command git symbolic-ref --quiet --short HEAD 2>/dev/null \
    || command git rev-parse --short HEAD 2>/dev/null)"

  if [[ -n "$(command git status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    __vicegerent_prompt_dirty='*'
  fi
  __vicegerent_prompt_git_context="${__vicegerent_prompt_repo} (${__vicegerent_prompt_branch}${__vicegerent_prompt_dirty})"
}

__vicegerent_prompt_set_title() {
  case ${TERM-} in
    xterm*|rxvt*|screen*|tmux*)
      local title=${__vicegerent_prompt_git_context:-${USER:-agent}@${HOSTNAME%%.*}}
      printf '\033]0;%s — %s\007' "$title" "$PWD"
      ;;
  esac
}

__vicegerent_prompt_command() {
  local exit_status=$?
  __vicegerent_prompt_update_git
  __vicegerent_prompt_status=''
  (( exit_status == 0 )) || __vicegerent_prompt_status="✗ $exit_status"
  __vicegerent_prompt_set_title
}

# Preserve an existing prompt hook when a base image or user configuration set
# one before this script. Our hook must run first to capture the last command's
# exit status rather than the status of the other hook.
if [[ -n ${PROMPT_COMMAND-} ]]; then
  PROMPT_COMMAND="__vicegerent_prompt_command; ${PROMPT_COMMAND}"
else
  PROMPT_COMMAND='__vicegerent_prompt_command'
fi

if [[ ${TERM-} == dumb || -n ${NO_COLOR-} ]]; then
  PS1='${__vicegerent_prompt_status:+${__vicegerent_prompt_status} }\u@\h ${__vicegerent_prompt_git_context:+${__vicegerent_prompt_git_context} }\w
> '
else
  PS1='\[\e[2m\]\u@\h\[\e[0m\] \[\e[1;36m\]${__vicegerent_prompt_git_context}\[\e[0m\] \[\e[2m\]\w\[\e[0m\]
\[\e[31m\]${__vicegerent_prompt_status:+${__vicegerent_prompt_status} }\[\e[0m\]\[\e[1;36m\]❯\[\e[0m\] '
fi
