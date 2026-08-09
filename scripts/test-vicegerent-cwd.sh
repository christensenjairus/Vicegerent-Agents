#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$WORK/linked-cli" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/caller"

cat > "$WORK/bin/python3" <<'EOF'
#!/usr/bin/env bash
printf '%s\t%s\n' "$PWD" "$1"
EOF
chmod +x "$WORK/bin/python3"

actual_from_unrelated_directory="$(
  cd "$WORK/caller"
  PATH="$WORK/bin:$PATH" "$REPO_ROOT/vicegerent" host-packages check
)"

expected="${REPO_ROOT}"$'\t'"${REPO_ROOT}/host/brew/reconcile.py"
if [[ "$actual_from_unrelated_directory" != "$expected" ]]; then
  printf 'vicegerent used %q; expected repository root and helper %q\n' \
    "$actual_from_unrelated_directory" "$expected" >&2
  exit 1
fi

git -C "$REPO_ROOT" worktree add --detach --quiet "$WORK/linked-cli" HEAD
cp "$REPO_ROOT/vicegerent" "$WORK/linked-cli/vicegerent"
ln -s "$WORK/linked-cli/vicegerent" "$WORK/bin/vicegerent"

actual_from_worktree="$(
  cd "$REPO_ROOT"
  PATH="$WORK/bin:$PATH" vicegerent host-packages check
)"

if [[ "$actual_from_worktree" != "$expected" ]]; then
  printf 'vicegerent used %q; expected caller worktree and helper %q\n' \
    "$actual_from_worktree" "$expected" >&2
  exit 1
fi
