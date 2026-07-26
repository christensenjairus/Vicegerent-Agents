#!/usr/bin/env bash
# Commit the skills tree to a local git repo so content destroyed through a
# published symlink can be recovered.
#
# The other harnesses reach Hermes' skills through symlinks
# (.claude/skills/<name> -> /opt/data/skills/<cat>/<name>). Both
# `rm -rf <link>/` and `rm -rf <link>/*` recurse THROUGH the link and delete
# the real content, silently, exit 0. Most skills ship in the image, but an
# agent-authored one exists only on this volume -- which is excluded from the
# Velero backup (backup-volumes-excludes in _sandbox.tpl).
#
# The git dir lives outside the tree so no .git artifact appears anywhere a
# harness walks. Restore with:
#   git --git-dir=/opt/data/.skills-snapshots.git \
#       --work-tree=/opt/data/skills checkout -- .
set -uo pipefail

skills="${HERMES_SKILLS_DIR:-/opt/data/skills}"
gitdir="${SKILLS_SNAPSHOT_GITDIR:-/opt/data/.skills-snapshots.git}"

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
git_snap diff --cached --quiet && exit 0

count="$(git_snap diff --cached --name-only | wc -l | tr -d ' ')"
body="$(git_snap diff --cached --name-status | head -50)"
git_snap commit -q -m "snapshot: ${count} path(s)" -m "${body}" || exit 0

echo "snapshot-skills: committed ${count} changed path(s)" >&2
