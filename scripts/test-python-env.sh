#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/python-env.sh
source "$REPO_ROOT/scripts/lib/python-env.sh"
TEST_SYSTEM_PYTHON="$(command -v python3)"
export TEST_SYSTEM_PYTHON

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/.venv/bin"
printf '%s\n' '[project]' 'name = "python-env-test"' 'version = "0.0.0"' 'requires-python = ">=3.11"' 'dependencies = ["uv==0.11.6"]' > "$WORK/pyproject.toml"
printf '%s\n' 'version = 1' > "$WORK/uv.lock"
touch "$WORK/.venv.lock"

cat > "$WORK/.venv/bin/uv" <<'UV'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  echo "uv 0.11.6"
  exit 0
fi
[[ -z "${UV_EXCLUDE_NEWER:-}" ]] || {
  echo "FAIL - uv inherited UV_EXCLUDE_NEWER" >&2
  exit 1
}
[[ -z "${UV_DEFAULT_INDEX:-}" ]] || {
  echo "FAIL - uv inherited UV_DEFAULT_INDEX" >&2
  exit 1
}
[[ -z "${UV_RESOLUTION:-}" ]] || {
  echo "FAIL - uv inherited UV_RESOLUTION" >&2
  exit 1
}
[[ " $* " == *" --no-config "* ]] || {
  echo "FAIL - uv did not disable user configuration" >&2
  exit 1
}
mkdir "$FAKE_UV_STATE/active" 2>/dev/null || {
  touch "$FAKE_UV_STATE/overlap"
  exit 1
}
sleep 1
rmdir "$FAKE_UV_STATE/active"
UV
chmod +x "$WORK/.venv/bin/uv"
printf '%s\n' '#!/usr/bin/env bash' 'echo repository-python' > "$WORK/.venv/bin/python3"
chmod +x "$WORK/.venv/bin/python3"
export FAKE_UV_STATE="$WORK"
export UV_DEFAULT_INDEX=https://packages.example.invalid/simple
export UV_EXCLUDE_NEWER=2026-08-02
export UV_RESOLUTION=lowest

mkdir -p "$WORK/external-venv/bin" "$WORK/system/bin"
printf '%s\n' '#!/usr/bin/env bash' 'echo "yq 3.2.3"' > "$WORK/external-venv/bin/yq"
printf '%s\n' '#!/usr/bin/env bash' 'echo "yq (https://github.com/mikefarah/yq/) version v4.53.3"' > "$WORK/system/bin/yq"
printf '%s\n' '#!/usr/bin/env bash' \
  "[[ \"\$#\" -eq 0 ]] && { echo system-python; exit; }" \
  "exec \"\$TEST_SYSTEM_PYTHON\" \"\$@\"" > "$WORK/system/bin/python3"
chmod +x "$WORK/external-venv/bin/yq" "$WORK/system/bin/yq" "$WORK/system/bin/python3"
PATH="$WORK/external-venv/bin:$WORK/system/bin:$PATH"

ensure_python_environment "$WORK" & first=$!
ensure_python_environment "$WORK" & second=$!
wait "$first"
wait "$second"
[[ ! -e "$WORK/overlap" ]] || { echo "FAIL - concurrent environment syncs overlapped" >&2; exit 1; }

ensure_python_environment "$WORK"
[[ "$(python3)" == "repository-python" ]] \
  || { echo "FAIL - repository environment did not replace cached python3" >&2; exit 1; }
[[ "$(command -v yq)" == "$WORK/.venv/vicegerent-bin/yq" ]] \
  || { echo "FAIL - repository environment did not isolate mikefarah/yq" >&2; exit 1; }
[[ "$(yq --version)" == *"mikefarah/yq"* ]] \
  || { echo "FAIL - isolated yq is not mikefarah/yq" >&2; exit 1; }

echo "PASS - Python environment reconciliation is serialized and isolated from host policy"
