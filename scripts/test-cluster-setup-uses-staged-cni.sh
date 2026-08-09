#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/bin"

cat > "$WORK/bin/kind" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat > "$WORK/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *'get configmap coredns'*) printf '.:53 {\n}\n' ;;
esac
EOF

cat > "$WORK/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  exec) printf '755\n' ;;
  run) printf '192.168.65.254 host.docker.internal\n' ;;
esac
EOF

cat > "$WORK/bin/cilium" <<'EOF'
#!/usr/bin/env bash
printf 'cluster setup unexpectedly invoked the legacy CNI bootstrap command\n' >&2
exit 97
EOF

chmod +x "$WORK/bin/kind" "$WORK/bin/kubectl" "$WORK/bin/docker" "$WORK/bin/cilium"

if ! output="$(PATH="$WORK/bin:$PATH" "$REPO_ROOT/vicegerent" setup cluster 2>&1)"; then
  printf 'setup cluster failed:\n%s\n' "$output" >&2
  exit 1
fi
