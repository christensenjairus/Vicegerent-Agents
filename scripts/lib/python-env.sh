#!/usr/bin/env bash

# Reconcile the repository's single Python environment from uv.lock, then expose
# it to the calling shell. Python owns bootstrap and locking because fcntl gives
# macOS and Linux a kernel-released lock with no stale PID state.
prefer_mikefarah_yq() {
  local venv="$1" shim_dir directory candidate version
  local -a path_entries
  shim_dir="$venv/vicegerent-bin"
  mkdir -p "$shim_dir"
  shim_dir="$(cd "$shim_dir" && pwd)"
  IFS=: read -r -a path_entries <<< "$PATH"
  for directory in "${path_entries[@]}"; do
    [[ -n "$directory" ]] || directory=.
    directory="$(cd "$directory" 2>/dev/null && pwd)" || continue
    [[ "$directory" != "$shim_dir" ]] || continue
    candidate="$directory/yq"
    [[ -x "$candidate" ]] || continue
    version="$("$candidate" --version 2>&1)" || continue
    [[ "$version" == *"mikefarah/yq"* ]] || continue
    ln -sfn "$candidate" "$shim_dir/yq"
    case ":$PATH:" in
      *":$shim_dir:"*) ;;
      *) export PATH="$shim_dir:$PATH" ;;
    esac
    hash -r
    return
  done
}

ensure_python_environment() {
  local repo_root="$1" venv helper_dir
  repo_root="$(cd "$repo_root" && pwd)"
  venv="$repo_root/.venv"
  helper_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  command -v python3 >/dev/null 2>&1 \
    || { echo "ERROR - python3 is not installed or not on PATH" >&2; return 1; }
  python3 "$helper_dir/python-env.py" "$repo_root" || return 1

  export VIRTUAL_ENV="$venv"
  case ":$PATH:" in
    *":$venv/bin:"*) ;;
    *) export PATH="$venv/bin:$PATH" ;;
  esac
  hash -r
  prefer_mikefarah_yq "$venv"
}
