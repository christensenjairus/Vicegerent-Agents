#!/usr/bin/env bash
# Run the host-side ghostunnel server in the foreground, reading its mTLS
# material from the local host directory that setup-secrets-platform.sh writes.
# The CA private key never enters Kubernetes -- the server cert/key are also
# mirrored into the ghostunnel-server Secret solely so a host missing this
# directory can recover them via ensure_ghostunnel_material (./vicegerent
# start). These files are the source of truth for the laptop side and are
# disposable - re-run setup-secrets-platform.sh to regenerate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/cli-ui.sh
source "$SCRIPT_DIR/../lib/cli-ui.sh"

GHOSTUNNEL_HOST_DIR="${GHOSTUNNEL_HOST_DIR:-$HOME/.vicegerent/ghostunnel}"
# Bind loopback only. Kind reaches this via host.docker.internal (Docker Desktop
# proxies it to the host's localhost); binding 0.0.0.0 would expose it to the LAN.
LISTEN="${LISTEN:-127.0.0.1:8453}"
TARGET="${TARGET:-127.0.0.1:4483}"
ALLOW_CN="${ALLOW_CN:-agent-client}"
GHOSTUNNEL="${GHOSTUNNEL:-ghostunnel}"

for f in server.crt server.key ca.cert; do
  if [[ ! -s "$GHOSTUNNEL_HOST_DIR/$f" ]]; then
    ui_error "Missing $GHOSTUNNEL_HOST_DIR/$f."
    ui_info "Run './vicegerent start' to recover it from the Kind ghostunnel-server Secret, or regenerate it:"
    ui_command "./vicegerent setup secrets platform"
    exit 1
  fi
done

# Run ghostunnel as a child and forward TERM/INT to it so supervisord's
# SIGTERM reaches ghostunnel directly.
"$GHOSTUNNEL" server \
  --listen "$LISTEN" \
  --target "$TARGET" \
  --cert "$GHOSTUNNEL_HOST_DIR/server.crt" \
  --key "$GHOSTUNNEL_HOST_DIR/server.key" \
  --cacert "$GHOSTUNNEL_HOST_DIR/ca.cert" \
  --allow-cn "$ALLOW_CN" &
GHOSTUNNEL_PID=$!
forward_signal() { kill -s "$1" "$GHOSTUNNEL_PID" 2>/dev/null || true; }
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT
wait "$GHOSTUNNEL_PID"
