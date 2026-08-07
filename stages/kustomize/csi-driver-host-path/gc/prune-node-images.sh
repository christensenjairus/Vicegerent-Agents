#!/bin/sh
# Removes container images no container on the node references any more.
# Project images (agent, mcp-cerbos-shim) are rebuilt under new tags often, and
# the node never garbage-collects the superseded ones on its own, so a
# long-lived cluster's containerd store only grows. The daily cadence is also
# the rollback window: an image an upgrade replaces stays until the next run.
set -eu

CRICTL=/host/bin/crictl
RUNTIME_ENDPOINT=unix:///run/containerd/containerd.sock

# crictl gives every CRI call a 2s deadline by default and issues the removals
# concurrently, so the largest image -- the multi-gigabyte agent, the one most
# worth reclaiming -- is the one that reliably misses it. Lift the per-call
# deadline and let a single wall-clock bound govern instead, sized to leave the
# reconcile its share of the job's activeDeadlineSeconds (its waits total 7m).
RPC_TIMEOUT=5m
DEADLINE=300

log() { echo "[csi-hostpath-gc] $*"; }

# Best-effort. This runs ahead of the CSI reconcile in the same pod, and with
# backoffLimit 0 a non-zero exit here would cost the cluster that run's volume
# GC as well.
status=0
output="$(timeout "$DEADLINE" "$CRICTL" --timeout "$RPC_TIMEOUT" \
  --runtime-endpoint "$RUNTIME_ENDPOINT" rmi --prune 2>&1)" || status=$?

# crictl swallows per-image removal failures under --prune and still exits 0,
# so report what it announced deleting rather than trusting the exit status.
if [ "$status" -eq 0 ]; then
  log "pruned $(printf '%s\n' "$output" | grep -c '^Deleted: ' || true) unused node image(s)"
else
  log "WARNING: image prune exited $status (124 is the ${DEADLINE}s deadline); continuing"
fi

[ -z "$output" ] || printf '%s\n' "$output"
