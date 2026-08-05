#!/bin/sh
# Root-owned global hook dispatcher, reached via core.hooksPath. That path replaces
# .git/hooks wholesale rather than merging, so every hook name is symlinked here and
# this script re-invokes the repo's own hook -- otherwise installing the guard would
# silently disable pre-commit (and every other hook) in every repo.
set -eu

hook="$(basename "$0")"

chain_repo_hook() {
    # core.hooksPath also redirects `git rev-parse --git-path hooks`, so the repo's
    # own hook dir has to be derived from the git dir itself.
    git_dir="$(git rev-parse --absolute-git-dir 2>/dev/null || true)"
    [ -n "${git_dir}" ] || return 0
    repo_hook="${git_dir}/hooks/${hook}"
    [ -x "${repo_hook}" ] || return 0
    if [ -n "${1:-}" ]; then
        shift_args_file="$1"
        shift
        "${repo_hook}" "$@" < "${shift_args_file}"
    else
        "${repo_hook}" "$@"
    fi
}

if [ "${hook}" = "pre-push" ]; then
    # Spool stdin: the protected-branch check consumes it, but a repo-local
    # pre-push still needs the original ref-update lines.
    spool="$(mktemp)"
    trap 'rm -f "${spool}"' EXIT INT TERM
    cat > "${spool}"

    protected='development main master production'
    blocked=''
    while read -r _local_ref local_sha remote_ref _remote_sha; do
        [ -n "${remote_ref:-}" ] || continue
        case "${remote_ref}" in
            refs/heads/*) branch="${remote_ref#refs/heads/}" ;;
            *) continue ;;
        esac
        for p in ${protected}; do
            [ "${branch}" = "${p}" ] || continue
            case "${local_sha}" in
                0000000000000000000000000000000000000000)
                    blocked="${blocked} delete:${branch}" ;;
                *) blocked="${blocked} update:${branch}" ;;
            esac
        done
    done < "${spool}"

    if [ -n "${blocked}" ]; then
        echo "vicegerent: BLOCKED push to protected branch —${blocked}" >&2
        echo "vicegerent: push a feature branch and open a merge request instead." >&2
        exit 1
    fi

    chain_repo_hook "${spool}" "$@"
    exit $?
fi

chain_repo_hook '' "$@"
