#!/bin/sh
# Ensures every Job pod starts with the CSI plugin running before image pruning.
set -eu

NS=csi-hostpath-system
STS=csi-hostpathplugin

log() { echo "[csi-hostpath-gc] $*"; }

attempt=1
while [ "$attempt" -le 3 ]; do
  if kubectl -n "$NS" patch statefulset "$STS" --type=merge -p '{"spec":{"replicas":1}}' \
    && kubectl -n "$NS" wait --for=condition=Ready pod/"$STS-0" --timeout=2m; then
    log "$STS is ready before image pruning"
    exit 0
  fi
  log "failed to restore $STS before image pruning (attempt $attempt/3)"
  attempt=$((attempt + 1))
  sleep 1
done

log "ERROR: $STS remains scaled down; refusing image prune"
exit 1
