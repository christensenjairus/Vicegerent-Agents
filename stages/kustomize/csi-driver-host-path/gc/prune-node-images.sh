#!/bin/sh
# Removes container images no container on the node references any more.
# Project images (agent, mcp-cerbos-shim) are rebuilt under new tags often, and
# the node never garbage-collects the superseded ones on its own, so a
# long-lived cluster's containerd store only grows. The daily cadence is also
# the rollback window: an image an upgrade replaces stays until the next run.
set -eu

CRICTL=/host/bin/crictl
RUNTIME_ENDPOINT=unix:///run/containerd/containerd.sock

log() { echo "[csi-hostpath-gc] $*"; }

# Best-effort. This runs ahead of the CSI reconcile in the same pod, and with
# backoffLimit 0 a non-zero exit here would cost the cluster that run's volume
# GC as well. A node with nothing to prune exits 0 too, so a failure is real.
if "$CRICTL" --runtime-endpoint "$RUNTIME_ENDPOINT" rmi --prune; then
  log "pruned unused node images"
else
  log "WARNING: image prune failed (continuing)"
fi
