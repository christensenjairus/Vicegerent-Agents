#!/usr/bin/env bash
# Exercises the git-guard against a real local remote. Every bypass vector must be
# blocked and every legitimate operation must still work. Run standalone or as the
# Dockerfile build-time smoke test.
#
# Success is asserted by inspecting the bare remote's refs directly, never by the
# push command's exit status -- a guard that prints a refusal but still pushes would
# otherwise read as a pass. Cleanup uses update-ref on the remote because the guard
# deliberately refuses to delete a protected branch.
set -uo pipefail

GUARD_DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"
WRAPPER="${GUARD_DIR}/git"
DISPATCH="${GUARD_DIR}/hook-dispatch.sh"
REAL_GIT=/usr/bin/git

pass=0
fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; fail=$((fail + 1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Root-owned layout mirror: a hooks dir with every hook name symlinked to the
# dispatcher, plus a system gitconfig standing in for /etc/gitconfig.
HOOKS="${WORK}/git-hooks"
mkdir -p "${HOOKS}"
cp "${DISPATCH}" "${HOOKS}/hook-dispatch.sh"
chmod +x "${HOOKS}/hook-dispatch.sh"
for h in pre-push pre-commit commit-msg prepare-commit-msg post-commit; do
    ln -sf hook-dispatch.sh "${HOOKS}/${h}"
done
printf '[core]\n\thooksPath = %s\n' "${HOOKS}" > "${WORK}/gitconfig"

# The installed wrapper injects an absolute TRUSTED_HOOKS path that does not exist
# outside the image, so point it at this harness's hooks dir for the test copy.
BIN="${WORK}/bin"
mkdir -p "${BIN}"
sed "s|^readonly TRUSTED_HOOKS=.*|readonly TRUSTED_HOOKS=${HOOKS}|" "${WRAPPER}" > "${BIN}/git"
chmod +x "${BIN}/git"
grep -q "TRUSTED_HOOKS=${HOOKS}" "${BIN}/git" \
    || { echo "harness broken: TRUSTED_HOOKS not rewritten"; exit 1; }

export GIT_CONFIG_SYSTEM="${WORK}/gitconfig"
export PATH="${BIN}:${PATH}"
# Isolate HOME and XDG: config-write cases would otherwise land in the real
# ~/.gitconfig, and a stray core.hooksPath there disarms the hook under test.
export HOME="${WORK}/home"
export XDG_CONFIG_HOME="${WORK}/xdg"
mkdir -p "${HOME}" "${XDG_CONFIG_HOME}"

REMOTE="${WORK}/remote.git"
REPO="${WORK}/repo"
"${REAL_GIT}" init -q --bare "${REMOTE}"
"${REAL_GIT}" init -q "${REPO}"
cd "${REPO}" || exit 1
"${REAL_GIT}" config user.email guard@test
"${REAL_GIT}" config user.name guard
"${REAL_GIT}" remote add origin "${REMOTE}"
echo seed > f
"${REAL_GIT}" add f
"${REAL_GIT}" commit -qm seed
"${REAL_GIT}" branch -M main
"${REAL_GIT}" branch feature/work

[ "$(command -v git)" = "${BIN}/git" ] \
    || { echo "harness broken: git is not the wrapper"; exit 1; }

# Ground truth: did the protected ref actually land on the remote?
remote_has() { "${REAL_GIT}" --git-dir="${REMOTE}" show-ref -q "refs/heads/$1"; }
reset_remote() {
    for b in main master production; do
        "${REAL_GIT}" --git-dir="${REMOTE}" update-ref -d "refs/heads/${b}" 2>/dev/null
    done
}

# Asserts the protected branch did NOT reach the remote, whatever the exit status.
blocked() {
    local name="$1"; shift
    local out branch=main
    reset_remote
    out="$("$@" 2>&1)"
    if remote_has "${branch}"; then
        no "${name}" "BYPASS -- ${branch} landed on the remote"
    elif grep -q 'vicegerent: BLOCKED' <<< "${out}"; then
        ok "${name}"
    else
        no "${name}" "not pushed, but no guard message: ${out%%$'\n'*}"
    fi
    reset_remote
}

# Same assertion, for cases the hook (not the wrapper) is expected to stop, where
# the refusal text comes from the hook and git reports a failed push.
blocked_any() {
    local name="$1"; shift
    reset_remote
    "$@" >/dev/null 2>&1
    if remote_has main; then
        no "${name}" "BYPASS -- main landed on the remote"
    else
        ok "${name}"
    fi
    reset_remote
}

allowed() {
    local name="$1"; shift
    local out
    out="$("$@" 2>&1)"
    if grep -q 'vicegerent: BLOCKED' <<< "${out}"; then
        no "${name}" "guard blocked a legitimate operation: ${out%%$'\n'*}"
    else
        ok "${name}"
    fi
}

echo "== bypass vectors (must be BLOCKED) =="
blocked "push HEAD:main"                  git push origin HEAD:main
blocked "push main"                       git push origin main
blocked "push refs/heads/main"            git push origin HEAD:refs/heads/main
blocked "push feature:main (renamed dst)" git push origin feature/work:main
blocked "push --no-verify HEAD:main"      git push --no-verify origin HEAD:main
blocked "push -f HEAD:main"               git push -f origin HEAD:main
blocked "push +HEAD:main (force refspec)" git push origin +HEAD:main
blocked "push --delete main"              git push --delete origin main
blocked "push --all"                      git push --all origin
blocked "push --mirror"                   git push --mirror origin
blocked "push master"                     git push origin HEAD:master
blocked "push production"                 git push origin HEAD:production
blocked "-c core.hooksPath= + push main"  git -c core.hooksPath=/dev/null push origin HEAD:main
blocked "-ccore.hooksPath= (glued)"       git -ccore.hooksPath=/dev/null push origin HEAD:main
blocked "--config-env core.hooksPath"     git --config-env core.hooksPath=EVIL push origin HEAD:main

echo "== F4: git config keys are case-insensitive (must be BLOCKED) =="
blocked "-c CORE.HOOKSPATH= + push main"  git -c CORE.HOOKSPATH=/dev/null push origin HEAD:main
blocked "-c Core.HooksPath= + push main"  git -c Core.HooksPath=/dev/null push origin HEAD:main
blocked "config --global CORE.HOOKSPATH"  git config --global CORE.HOOKSPATH /dev/null

echo "== env-injection bypasses (must be BLOCKED) =="
blocked "GIT_CONFIG_COUNT hooksPath" \
    env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null \
        git push origin HEAD:main
blocked "GIT_CONFIG_NOSYSTEM=1"      env GIT_CONFIG_NOSYSTEM=1 git push origin HEAD:main
blocked "GIT_CONFIG_GLOBAL override" env GIT_CONFIG_GLOBAL=/dev/null git push origin HEAD:main

echo "== F3: GIT_CONFIG_SYSTEM must not be honoured (must be BLOCKED) =="
blocked "GIT_CONFIG_SYSTEM=/dev/null"        env GIT_CONFIG_SYSTEM=/dev/null git push origin HEAD:main
printf '[core]\n\thooksPath = %s/nowhere\n' "${WORK}" > "${WORK}/evil-system"
blocked "GIT_CONFIG_SYSTEM=<attacker file>"  env GIT_CONFIG_SYSTEM="${WORK}/evil-system" git push origin HEAD:main

echo "== F2: writable config outranks system scope (must be BLOCKED) =="
# Each case sets hooksPath somewhere agent-writable AND supplies the destination via
# config, so neither layer can rely on the other. The wrapper's command-scope
# injection is what must hold.
mkdir -p "${XDG_CONFIG_HOME}/git" "${WORK}/nowhere"
printf '[core]\n\thooksPath = %s/nowhere\n' "${WORK}" > "${XDG_CONFIG_HOME}/git/config"
"${REAL_GIT}" checkout -q feature/work
"${REAL_GIT}" config remote.origin.push HEAD:main
blocked_any "XDG hooksPath + remote.origin.push" git push origin
"${REAL_GIT}" config remote.origin.push 'refs/heads/*:refs/heads/*'
blocked_any "XDG hooksPath + glob refspec"       git push origin
"${REAL_GIT}" config --unset remote.origin.push
rm -f "${XDG_CONFIG_HOME}/git/config"

printf '[core]\n\thooksPath = %s/nowhere\n' "${WORK}" > "${WORK}/inc.cfg"
printf '[include]\n\tpath = %s/inc.cfg\n' "${WORK}" > "${HOME}/.gitconfig"
"${REAL_GIT}" config remote.origin.push HEAD:main
blocked_any "global gitconfig include hooksPath" git push origin
rm -f "${HOME}/.gitconfig"

"${REAL_GIT}" config core.hooksPath "${WORK}/nowhere"
blocked_any "repo-local core.hooksPath"          git push origin
"${REAL_GIT}" config --unset core.hooksPath

"${REAL_GIT}" config extensions.worktreeConfig true
printf '[core]\n\thooksPath = %s/nowhere\n' "${WORK}" > "${REPO}/.git/config.worktree"
blocked_any "worktree config hooksPath"          git push origin
rm -f "${REPO}/.git/config.worktree"
"${REAL_GIT}" config --unset extensions.worktreeConfig
"${REAL_GIT}" config --unset remote.origin.push

echo "== F1: aliases must not launder a push (must be BLOCKED) =="
"${REAL_GIT}" config alias.ship 'push --no-verify origin HEAD:main'
blocked "alias -> push --no-verify"     git ship
"${REAL_GIT}" config alias.deploy 'push origin HEAD:main'
blocked "alias -> push main"            git deploy
"${REAL_GIT}" config alias.nested 'ship'
blocked "nested alias -> push main"     git nested
"${REAL_GIT}" config alias.shellish '!/usr/bin/git push origin HEAD:main'
blocked "shell alias refused"           git shellish
"${REAL_GIT}" config alias.loop1 'loop2'
"${REAL_GIT}" config alias.loop2 'loop1'
reset_remote
timeout 20 git loop1 >/dev/null 2>&1
if [ $? -eq 124 ]; then
    no "cyclic alias terminates" "wrapper hung on an alias cycle"
else
    ok "cyclic alias terminates"
fi
for a in ship deploy nested shellish loop1 loop2; do
    "${REAL_GIT}" config --unset "alias.${a}"
done

echo "== F5: repo-location options must not desync branch lookup (BLOCKED) =="
mkdir -p "${WORK}/elsewhere"
printf '[core]\n\thooksPath = %s/nowhere\n' "${WORK}" > "${XDG_CONFIG_HOME}/git/config"
"${REAL_GIT}" -C "${REPO}" checkout -q main
blocked_any "-C <repo> bare push on main" \
    env -C "${WORK}/elsewhere" git -C "${REPO}" -c push.default=current push origin
blocked_any "--git-dir/--work-tree bare push on main" \
    env -C "${WORK}/elsewhere" git --git-dir="${REPO}/.git" --work-tree="${REPO}" \
        -c push.default=current push origin
rm -f "${XDG_CONFIG_HOME}/git/config"
"${REAL_GIT}" -C "${REPO}" checkout -q feature/work

echo "== bare push while ON a protected branch (must be BLOCKED) =="
"${REAL_GIT}" symbolic-ref -q HEAD refs/heads/main >/dev/null
blocked "bare 'git push' on main" git -c push.default=current push origin
"${REAL_GIT}" symbolic-ref -q HEAD refs/heads/feature/work >/dev/null

echo "== config tampering (must be BLOCKED) =="
blocked "config --global core.hooksPath" git config --global core.hooksPath /dev/null
blocked "config core.hooksPath (local)"  git config core.hooksPath /dev/null
allowed "config --get core.hooksPath"         git config --get core.hooksPath
allowed "config --show-origin core.hooksPath" git config --show-origin core.hooksPath

echo "== process audit noise (must not execute /usr/bin/[) =="
PROBE_BIN="${WORK}/probe-bin"
BRACKET_EXEC_LOG="${WORK}/bracket-execs"
mkdir -p "${PROBE_BIN}"
# shellcheck disable=SC2016 # The probe must preserve these variables for its child shell.
printf '%s\n' \
    '#!/bin/sh' \
    'printf "%s\n" "$*" >> "${BRACKET_EXEC_LOG:?}"' \
    'exec /usr/bin/[ "$@"' > "${PROBE_BIN}/["
printf 'enable -n "["\n' > "${WORK}/disable-bracket.sh"
chmod +x "${PROBE_BIN}/["
: > "${BRACKET_EXEC_LOG}"
BASH_ENV="${WORK}/disable-bracket.sh" BRACKET_EXEC_LOG="${BRACKET_EXEC_LOG}" \
    PATH="${PROBE_BIN}:${PATH}" git worktree list --porcelain >/dev/null
if [[ -s "${BRACKET_EXEC_LOG}" ]]; then
    no "worktree list avoids external test" "wrapper executed: $(tr '\n' ';' < "${BRACKET_EXEC_LOG}")"
else
    ok "worktree list avoids external test"
fi

echo "== legitimate operations (must be ALLOWED) =="
"${REAL_GIT}" checkout -q feature/work
allowed "push feature branch"         git push -q origin feature/work
allowed "push HEAD:feature/other"     git push -q origin HEAD:feature/other
allowed "push --delete feature/other" git push -q --delete origin feature/other
allowed "push a tag" sh -c '/usr/bin/git tag -f v-guard-test >/dev/null && git push -q -f origin refs/tags/v-guard-test'
allowed "push -o with a value"         git push -q -o ci.skip origin HEAD:feature/pushopt
allowed "status"                       git status --short
allowed "log"                          git log --oneline -1
allowed "config --global write"         git config --global guard.probe 1
allowed "fetch"                         git fetch -q origin
allowed "branch containing 'main'"      git push -q origin HEAD:feature/maintenance
allowed "branch named 'mainline'"       git push -q origin HEAD:mainline
allowed "benign alias still works" sh -c '/usr/bin/git config alias.st status && git st --short'
allowed "-C <repo> feature push"        git -C "${REPO}" push -q origin HEAD:feature/viaC

echo "== hook layer alone, wrapper bypassed via ${REAL_GIT} (must be BLOCKED) =="
reset_remote
"${REAL_GIT}" push origin HEAD:main >/dev/null 2>&1
if remote_has main; then
    no "real git push HEAD:main" "hook did not fire"
else
    ok "real git push HEAD:main caught by global pre-push hook"
fi
reset_remote

# Paths that reach git without traversing the PATH wrapper. None of them touches
# core.hooksPath, so the hook must still fire -- that is the property under test,
# and it is what keeps these out of the "known bypass" class.
SHADOW="${WORK}/shadowbin"
mkdir -p "${SHADOW}"
ln -sf "${REAL_GIT}" "${SHADOW}/git"
blocked_any "PATH-shadowed wrapper (symlink to real git)" \
    env PATH="${SHADOW}:${PATH}" git push origin HEAD:main
blocked_any "shell function named git calling real git" \
    bash -c 'git(){ /usr/bin/git "$@"; }; git push origin HEAD:main'
blocked_any "/usr/lib/git-core/git hardlink" \
    /usr/lib/git-core/git push origin HEAD:main

if [ -x /opt/hermes/.venv/bin/python ] \
    && /opt/hermes/.venv/bin/python -c 'import git' 2>/dev/null; then
    blocked_any "GitPython push (resolves git via PATH)" \
        /opt/hermes/.venv/bin/python -c \
        "import git; git.Repo('${REPO}').remote('origin').push('HEAD:refs/heads/main')"
    blocked_any "GitPython pinned to real git executable" \
        env GIT_PYTHON_GIT_EXECUTABLE="${REAL_GIT}" /opt/hermes/.venv/bin/python -c \
        "import git; git.Repo('${REPO}').remote('origin').push('HEAD:refs/heads/main')"
fi

echo "== hook chaining: repo-local hooks must still run =="
mkdir -p "${REPO}/.git/hooks"
printf '#!/bin/sh\necho REPO-PRECOMMIT-RAN >&2\nexit 0\n' > "${REPO}/.git/hooks/pre-commit"
chmod +x "${REPO}/.git/hooks/pre-commit"
echo chain >> f
"${REAL_GIT}" add f
out="$("${REAL_GIT}" commit -m chain 2>&1)"
if grep -q 'REPO-PRECOMMIT-RAN' <<< "${out}"; then
    ok "repo-local pre-commit still runs under global hooksPath"
else
    no "repo-local pre-commit" "chaining broken: ${out}"
fi

# F6: pre-push was returning before the chaining branch, so a repo's own pre-push
# was silently dropped. It must run AND receive the original stdin ref lines.
# shellcheck disable=SC2016  # $* / $l must reach the generated hook unexpanded
printf '#!/bin/sh\necho "REPO-PREPUSH-RAN args=$*" >&2\nwhile read -r l; do echo "REPO-PREPUSH-STDIN $l" >&2; done\nexit 0\n' \
    > "${REPO}/.git/hooks/pre-push"
chmod +x "${REPO}/.git/hooks/pre-push"
out="$(git push origin HEAD:feature/chain 2>&1)"
if grep -q 'REPO-PREPUSH-RAN' <<< "${out}"; then
    ok "repo-local pre-push is chained (F6)"
else
    no "repo-local pre-push chaining" "hook did not run: ${out}"
fi
if grep -qE 'REPO-PREPUSH-STDIN .* refs/heads/feature/chain' <<< "${out}"; then
    ok "repo-local pre-push receives original stdin refspec lines (F6)"
else
    no "repo-local pre-push stdin" "stdin not replayed: ${out}"
fi
if grep -q 'REPO-PREPUSH-RAN args=origin' <<< "${out}"; then
    ok "repo-local pre-push receives original args (F6)"
else
    no "repo-local pre-push args" "args not forwarded: ${out}"
fi

# A failing repo-local pre-push must still be able to veto a legitimate push.
printf '#!/bin/sh\nexit 1\n' > "${REPO}/.git/hooks/pre-push"
chmod +x "${REPO}/.git/hooks/pre-push"
if git push origin HEAD:feature/veto >/dev/null 2>&1; then
    no "repo-local pre-push can veto" "failing repo hook did not block the push"
else
    ok "repo-local pre-push can veto a push"
fi
rm -f "${REPO}/.git/hooks/pre-push"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" -eq 0 ]
