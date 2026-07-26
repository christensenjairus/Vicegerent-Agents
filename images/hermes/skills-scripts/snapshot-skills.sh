#!/usr/bin/env bash
# Commit the skills tree to a local git repo so content destroyed through a
# published symlink can be recovered.
#
# The other harnesses reach Hermes' skills through symlinks
# (.claude/skills/<name> -> /opt/data/skills/<cat>/<name>). Both
# `rm -rf <link>/` and `rm -rf <link>/*` recurse THROUGH the link and delete
# the real content, silently, exit 0. Most skills ship in the image, but an
# agent-authored one exists only on this volume. The nightly Velero backup does
# cover it (only the models PVC carries velero.io/exclude-from-backup), but a
# once-a-day snapshot is far coarser than the per-skill_manage history here.
#
# The git dir lives outside the tree so no .git artifact appears anywhere a
# harness walks. Restore with:
#   git --git-dir=/opt/data/.skills-snapshots.git \
#       --work-tree=/opt/data/skills checkout -- .
set -uo pipefail

skills="${HERMES_SKILLS_DIR:-/opt/data/skills}"
gitdir="${SKILLS_SNAPSHOT_GITDIR:-/opt/data/.skills-snapshots.git}"
retain_days="${SKILLS_SNAPSHOT_RETAIN_DAYS:-7}"

[ -d "${skills}" ] || exit 0

if [ ! -d "${gitdir}" ]; then
  git init -q --bare "${gitdir}" || exit 0
  git --git-dir="${gitdir}" config core.excludesFile /dev/null
  # Bookkeeping/cache state churns on every skill read; snapshotting it would
  # commit on every turn and bury real content changes. .archive is kept --
  # a curator-archived skill is exactly the content worth recovering.
  printf '%s\n' .usage.json .usage.json.lock .curator_state \
    .bundled_manifest .hub/ > "${gitdir}/info/exclude"
fi

git_snap() {
  git --git-dir="${gitdir}" --work-tree="${skills}" \
    -c user.name=vicegerent -c user.email=vicegerent@localhost \
    -c commit.gpgsign=false "$@"
}

git_snap add -A || exit 0

# Commit only when something changed, but ALWAYS fall through to retention --
# an early exit here would mean a repo that stops changing never prunes, and
# stale history would sit there forever.
if git_snap diff --cached --quiet; then
  count=""
else
  count="$(git_snap diff --cached --name-only | wc -l | tr -d ' ')"
  body="$(git_snap diff --cached --name-status | head -50)"
  git_snap commit -q -m "snapshot: ${count} path(s)" -m "${body}" || exit 0
fi

git_snap rev-parse --verify -q HEAD >/dev/null 2>&1 || exit 0

# Retention: keep the last ${retain_days} days, matching the Velero schedule's
# ttl. Old commits stay reachable from HEAD, so gc alone reclaims nothing --
# the branch has to be rebuilt. Everything before the window collapses into one
# baseline commit that still holds the COMPLETE tree (not a diff), so the oldest
# recoverable state is preserved; only the intermediate steps are dropped.
cutoff="$(date -u -d "@$(( $(date +%s) - retain_days * 86400 ))" \
  +%Y-%m-%dT%H:%M:%S 2>/dev/null || true)"
base="$([ -n "${cutoff}" ] && git_snap rev-list -1 --before="${cutoff}" HEAD || true)"

# Rebuild only when it changes something: either several pre-window commits
# collapse into one, or the single pre-window commit is not already a synthetic
# baseline. Comparing against the root is wrong -- a lone stale root is exactly
# the case that must still be rewritten once newer commits exist.
stale_count="$([ -n "${base}" ] && git_snap rev-list --count "${base}" || echo 0)"
base_is_baseline=no
case "$(git_snap log -1 --pretty=%s "${base:-HEAD}" 2>/dev/null)" in
  "snapshot: baseline"*) base_is_baseline=yes ;;
esac

if [ -n "${base}" ] && { [ "${stale_count}" -gt 1 ] || [ "${base_is_baseline}" = no ]; }; then
  parent="$(git_snap commit-tree "${base}^{tree}" \
    -m "snapshot: baseline (history before the ${retain_days}-day window)")"
  for c in $(git_snap rev-list --reverse --since="${cutoff}" HEAD); do
    d="$(git_snap log -1 --pretty=%aI "${c}")"
    parent="$(GIT_AUTHOR_DATE="${d}" GIT_COMMITTER_DATE="${d}" git_snap commit-tree \
      "${c}^{tree}" -p "${parent}" -m "$(git_snap log -1 --pretty=%B "${c}")")"
  done
  if [ -n "${parent}" ]; then
    git_snap update-ref HEAD "${parent}"
    git_snap reflog expire --expire=now --all 2>/dev/null || true
    git_snap gc --prune=now -q 2>/dev/null || true
  fi
fi

[ -n "${count}" ] && echo "snapshot-skills: committed ${count} changed path(s)" >&2
exit 0
