#!/bin/sh
# Removes hostpath CSI volume directories and snapshot files under
# /csi-data-dir that no longer back any live PV or VolumeSnapshotContent.
set -eu

NS=csi-hostpath-system
STS=csi-hostpathplugin
DATA_DIR=/csi-data-dir
STATE_FILE="$DATA_DIR/state.json"
# Skip anything this recent: an in-flight operation may not be visible yet
# as a PV or state.json entry.
GRACE_MINUTES=360

log() { echo "[csi-hostpath-gc] $*"; }

live_pv_handles="$(kubectl get pv -o jsonpath='{range .items[*]}{.spec.csi.volumeHandle}{"\n"}{end}' | sort -u)"
live_vsc_handles="$(kubectl get volumesnapshotcontents.snapshot.storage.k8s.io \
  -o jsonpath='{range .items[*]}{.spec.source.snapshotHandle}{"\n"}{.status.snapshotHandle}{"\n"}{end}' \
  | sed '/^$/d' | sort -u)"
state_snapshot_ids="$(jq -r '.Snapshots[]?.Id // empty' "$STATE_FILE" | sort -u)"

is_in() {
  set_str=$1
  needle=$2
  printf '%s\n' "$set_str" | grep -qxF "$needle"
}

older_than_grace() {
  [ -z "$(find "$1" -maxdepth 0 -mmin +"$GRACE_MINUTES" 2>/dev/null)" ] && return 1 || return 0
}

# --- Phase 1: orphaned volume directories tracked in state.json.
#
# NodeID is not a "currently in use" signal: this driver sets it on every
# volume at creation regardless of current mount state. A live PV can also
# legitimately have empty Staged/Published (bound but unmounted, e.g. a kept
# rollback PVC). The only reliable check is whether a live PV's volumeHandle
# references this VolID.

candidate_vol_ids="$(jq -r '.Volumes[]?.VolID' "$STATE_FILE")"
stale_vol_ids=""
for vol_id in $candidate_vol_ids; do
  [ -z "$vol_id" ] && continue
  is_in "$live_pv_handles" "$vol_id" && continue
  vol_path="$DATA_DIR/$vol_id"
  [ -d "$vol_path" ] || continue
  older_than_grace "$vol_path" || continue
  stale_vol_ids="$stale_vol_ids $vol_id"
done
stale_vol_ids="$(printf '%s' "$stale_vol_ids" | xargs -n1 2>/dev/null | sort -u | xargs)"

if [ -n "$stale_vol_ids" ]; then
  log "orphaned volume directories: $stale_vol_ids"
  # Pausing avoids racing the plugin's own state.json writes.
  kubectl -n "$NS" patch statefulset "$STS" --type=merge -p '{"spec":{"replicas":0}}'
  kubectl -n "$NS" wait --for=delete pod/"$STS-0" --timeout=2m || true

  cp "$STATE_FILE" "$STATE_FILE.bak-$(date +%Y%m%d%H%M%S 2>/dev/null || echo pre-gc)"

  # Drop exactly stale_vol_ids, not "everything not backing a live PV": a
  # fresh, still-grace-period-protected entry must keep its state.json row
  # even though it isn't a live PV yet, or the driver loses track of a
  # volume whose directory this run correctly left alone.
  remove_json="$(printf '%s\n' "$stale_vol_ids" | tr ' ' '\n' | jq -R . | jq -s .)"
  jq --argjson remove "$remove_json" \
    '.Volumes = [ .Volumes[] | select(([.VolID] | inside($remove)) | not) ]' \
    "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"

  for vol_id in $stale_vol_ids; do
    log "removing orphaned volume directory $vol_id"
    rm -rf "$DATA_DIR/${vol_id:?}"
  done

  kubectl -n "$NS" patch statefulset "$STS" --type=merge -p '{"spec":{"replicas":1}}'
  kubectl -n "$NS" wait --for=condition=Ready pod/"$STS-0" --timeout=5m
  log "$STS back up"
else
  log "no orphaned volume directories"
fi

# --- Phase 2: orphaned snapshot files. This driver never tracks them in
# state.json's Snapshots list, so no pause is needed to remove them.

removed_snap_count=0
for snap_file in "$DATA_DIR"/*.snap; do
  [ -e "$snap_file" ] || continue
  snap_id="$(basename "$snap_file" .snap)"
  is_in "$live_vsc_handles" "$snap_id" && continue
  is_in "$state_snapshot_ids" "$snap_id" && continue
  older_than_grace "$snap_file" || continue
  log "removing orphaned snapshot file $snap_file"
  rm -f "$snap_file"
  removed_snap_count=$((removed_snap_count + 1))
done
[ "$removed_snap_count" -eq 0 ] && log "no orphaned snapshot files"

log "done"
