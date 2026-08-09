#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/workspace" "$work/index"
log="$work/calls.log"

cat > "$work/bin/zoekt-git-index" <<'EOF'
#!/usr/bin/env bash
printf 'index:%s\n' "$*" >> "$REPO_MAP_TEST_LOG"
EOF
cat > "$work/bin/zoekt" <<'EOF'
#!/usr/bin/env bash
printf 'search:%s\n' "$*" >> "$REPO_MAP_TEST_LOG"
EOF
cat > "$work/bin/zoekt-local-sync" <<'EOF'
#!/usr/bin/env bash
printf 'list:%s\n' "$*" >> "$REPO_MAP_TEST_LOG"
EOF
chmod +x "$work/bin"/*

for name in alpha beta; do
  git -C "$work/workspace" init -q "$name"
done
mkdir -p "$work/workspace/alpha/.worktrees/nested"
printf 'not a direct clone\n' > "$work/workspace/alpha/.worktrees/nested/.git"

export PATH="$work/bin:$PATH"
export REPO_MAP_TEST_LOG="$log"
export REPO_MAP_WORKSPACE="$work/workspace"
export ZOEKT_INDEX_DIR="$work/index"

bash "$repo_root/images/agent/repo-map" index --all >/dev/null
[[ "$(grep -c '^index:' "$log")" == 2 ]]
grep -q "$work/workspace/alpha" "$log"
grep -q "$work/workspace/beta" "$log"
if grep -q '/.worktrees/nested' "$log"; then
  echo 'repo-map indexed a nested worktree during default discovery' >&2
  exit 1
fi

: > "$log"
(
  cd "$work/workspace/alpha"
  bash "$repo_root/images/agent/repo-map" index >/dev/null
)
[[ "$(grep -c '^index:' "$log")" == 1 ]]
grep -q "$work/workspace/alpha" "$log"

bash "$repo_root/images/agent/repo-map" search 'symbol foo' >/dev/null
grep -q "search:-index_dir $work/index symbol foo" "$log"

bash "$repo_root/images/agent/repo-map" list >/dev/null
grep -q "list:list -index $work/index" "$log"

if bash "$repo_root/images/agent/repo-map" index "$work/workspace/missing" >/dev/null 2>&1; then
  echo 'repo-map accepted a non-repository path' >&2
  exit 1
fi

if ZOEKT_INDEX_DIR="$work/no-index" bash "$repo_root/images/agent/repo-map" search foo >/dev/null 2>&1; then
  echo 'repo-map searched without an index' >&2
  exit 1
fi

bash "$repo_root/images/agent/repo-map" --help | grep -q 'committed Git content'
printf 'PASS - repo-map indexes direct clones only and routes search/list to the local Zoekt index\n'
