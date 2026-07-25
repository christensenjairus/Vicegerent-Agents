# Backup and restore

| Failure | Procedure |
|---|---|
| An agent lost files or has a corrupted repository | [Restore one agent's volumes](#restore-one-agents-volumes) |
| An install deleted or damaged cluster objects | [Restore objects without touching volumes](#restore-objects-without-touching-volumes) |
| The cluster is unrecoverable | [Restore the whole cluster](#restore-the-whole-cluster) |

Install the `velero` CLI with `brew install velero`, select the cluster, and complete the [`/etc/hosts` fix](#etchosts-must-resolve-hostdockerinternal) before the first Velero command:

```bash
kubectl config use-context kind-vicegerent
```

## How it works, and why it's built this way

| Component | Role | Where it's configured |
|---|---|---|
| `csi-driver-host-path` | Backs `csi-hostpath-sc` and provides snapshots. | `stages/kustomize/csi-driver-host-path/` |
| `snapshot-controller` | Turns `VolumeSnapshot` requests into driver snapshots. | `stages/kustomize/snapshot-controller/` |
| `VolumeSnapshotClass` | Tells Velero which snapshot class to use. | `stages/kustomize/csi-driver-host-path/volumesnapshotclass.yaml` |
| Velero server | Orchestrates backups and restores and writes object manifests to S3. | `stages/values/velero.yaml` |
| Velero node-agent | Uses kopia to upload snapshot contents. | `deployNodeAgent: true` |
| rclone S3 | Serves the host backup directory as an S3 bucket. | `scripts/rclone/rclone-s3.sh` |

The CSI, Velero data-movement, and rclone path is used instead of copying a hostPath from macOS:

| Reason | Consequence |
|---|---|
| Portability | A hostPath copy assumes the node filesystem is the laptop filesystem. That fails if the node moves into a VM, such as minikube for gVisor kernel isolation, or onto a remote host. CSI and Velero use Kubernetes and CSI APIs identically in every topology. This is the primary reason. |
| Consistency | A live `rsync` can tear a SQLite database or mid-write Git repository; a CSI snapshot is point-in-time. |
| No duplication | Kopia is content-addressed and deduplicating, and the CSI snapshot is deleted after upload. On this cluster, the first `gitrepos` upload moved 8.2 GB and the next incremental upload moved 2.2 MB. |
| Supported recovery | Velero restores in dependency order with a status object. The same backup contains all Kubernetes manifests, making it the cluster-wide failsafe. |

Data movement is mandatory here. `csi-hostpath` stores snapshots inside the Kind node container, so `kind delete cluster` destroys a CSI snapshot. `configuration.defaultSnapshotMoveData: true` uploads its bytes to the rclone bucket on the host. Uploading temporarily provisions a PVC from each snapshot, so the node needs room for a second copy of the volume data.

The daily backup covers the full cluster:

```yaml
includedNamespaces: ['*']
includeClusterResources: true
```

This captures namespaced objects, CRDs, ClusterRoles, StorageClasses, PVs, and Helm release Secrets. After restoration, `helm list -A` therefore reports releases as `deployed`. A botched `./vicegerent install` is recoverable from the backup; the last verified full backup contained 1182 items.

## What is and isn't backed up

| Volume | Data backed up? | Reason |
|---|---|---|
| `data-<agent>` | Yes | Agent state |
| `gitrepos-<agent>` | Yes | Working trees and uncommitted work |
| `models-<agent>` | No | The `seed-data` initContainer reseeds it from the image. `charts/agent/templates/pvc.yaml` |
| victoria-logs `server-volume-…` | No | A reinstall reconstructs the seven-day observability window. `stages/values/victoria-logs.yaml` |

The two exclusions use `velero.io/exclude-from-backup: 'true'` on the claim, not the PV. Velero's CSI snapshot and data movement are a PVC-level `BackupItemAction`. This relies on the PVs having a `Delete` reclaim policy; if they change to `Retain`, exclude the PVs too. Excluding a claim omits both its data and PVC object, so the next install recreates an empty models claim for reseeding.

### Verify an exclusion

PVC annotations such as `velero.io/backup-name` and `velero.io/restore-name` are stamped by a restore. They describe the PVC's origin and do not change during later backups. Verify the runtime result with `DataUpload` objects matching the backup and current PVC UID:

```bash
BACKUP=velero-vicegerent-daily-20260725101005
AGENT=hermes

kubectl -n agent-sandbox get pvc "models-$AGENT" -o jsonpath='{.metadata.uid}{"\n"}'
kubectl -n velero get datauploads -l "velero.io/backup-name=$BACKUP" \
  -o custom-columns='NAME:.metadata.name,PVC_UID:.metadata.labels.velero\.io/pvc-uid,STATUS:.status.phase'
kubectl -n velero get backup "$BACKUP" \
  -o jsonpath='{.status.backupItemOperationsCompleted}/{.status.backupItemOperationsAttempted}{"\n"}'
```

No `DataUpload` for that PVC UID proves it was excluded. With one agent and both exclusions, expect two completed item operations.

`DataUpload` and `DataDownload` use `velero.io/v2alpha1`, not `v1`. Querying them under `v1` returns `no matches for kind`, which can be mistaken for evidence that no transfer occurred.

## The backup schedule

The schedule is under `schedules.vicegerent-daily` in `stages/values/velero.yaml`:

| Setting | Value |
|---|---|
| In-cluster object name | `velero-vicegerent-daily`; Helm prefixes the release name, while the values key is `vicegerent-daily`. The CLI requires the object name. |
| Time | `CRON_TZ=America/Denver 0 13 * * *` |
| Retention | `ttl: 168h` (seven days) |
| Backup names | `velero-vicegerent-daily-<UTC timestamp>` |

Garbage collection removes expired backups from the cluster and bucket automatically.

```bash
velero schedule get
velero schedule describe velero-vicegerent-daily
```

## Take and verify a backup

An ad-hoc backup inherits the server defaults: full-cluster scope, snapshot data movement, and claim exclusions.

```bash
BACKUP="pre-upgrade-$(date +%Y%m%d)"
velero backup create "$BACKUP" --wait
```

To set the full scope and a 30-day TTL explicitly:

```bash
BACKUP="pre-upgrade-$(date +%Y%m%d)"
velero backup create "$BACKUP" \
  --include-namespaces '*' \
  --include-cluster-resources \
  --ttl 720h \
  --wait
```

A `Completed` backup can contain zero volume data. Never rely on phase alone:

```bash
velero backup describe "$BACKUP" --details
kubectl -n velero get datauploads -l "velero.io/backup-name=$BACKUP"
du -sh ~/.vicegerent/rclone-s3/vicegerent
velero backup logs "$BACKUP" | grep -i 'level=warning'
```

Expect one completed `DataUpload` per included volume and a Data Movement entry for each. About 26 warnings per backup, mostly for unbackupable API endpoints, is the current baseline; investigate a jump.

## Restore one agent's volumes

Velero's default existing-resource policy is `none`: it skips an existing PVC rather than overwriting it. `--existing-resource-policy=update` does not repopulate a bound PVC. Delete only the claims being restored.

```bash
AGENT=hermes
BACKUP=velero-vicegerent-daily-20260725101005
RESTORE="restore-$AGENT-data-$(date +%Y%m%d%H%M)"

velero backup get

# Unmount the volume. The chart owns the PVCs, so Sandbox deletion does not delete them.
kubectl -n agent-sandbox delete sandbox "$AGENT"

# Destructive: delete only the claim being restored.
kubectl -n agent-sandbox delete pvc "data-$AGENT"

velero restore create "$RESTORE" \
  --from-backup "$BACKUP" \
  --include-namespaces agent-sandbox \
  --include-resources persistentvolumeclaims \
  --wait

kubectl -n velero get datadownloads
velero restore describe "$RESTORE" --details
kubectl -n agent-sandbox get pvc
./vicegerent install --stage agents
```

### Do not include `VolumeSnapshot` resources

Commands passed around for this procedure may include `volumesnapshots.snapshot.storage.k8s.io` and `volumesnapshotcontents.snapshot.storage.k8s.io`. They restore nothing on this cluster. With `snapshotMoveData: true`, Velero injects both kinds into every backup's `excludedResources` because snapshots are transient staging deleted after kopia uploads them; restoring one would point to a nonexistent `VolumeSnapshotContent`.

```bash
kubectl -n velero get backup "$BACKUP" -o jsonpath='{.spec.excludedResources}{"\n"}'
```

`persistentvolumeclaims` alone triggers the PVC restore item action, which creates a `DataDownload` and fills the newly provisioned volume from kopia. `persistentvolumes` is deliberately absent because `csi-hostpath-sc` dynamically provisions a new PV.

## Restore objects without touching volumes

These restores replace missing objects while leaving existing objects and volume data untouched.

```bash
BACKUP=velero-vicegerent-daily-20260725101005

# Restore selected cluster-scoped objects.
velero restore create "repair-cluster-objects-$(date +%Y%m%d%H%M)" \
  --from-backup "$BACKUP" \
  --include-cluster-resources \
  --include-resources customresourcedefinitions,clusterroles,clusterrolebindings,storageclasses \
  --wait

# Or restore one namespace without its volumes.
velero restore create "repair-ns-$(date +%Y%m%d%H%M)" \
  --from-backup "$BACKUP" \
  --include-namespaces agent-sandbox \
  --exclude-resources persistentvolumeclaims,persistentvolumes \
  --wait

./vicegerent install
```

## Restore the whole cluster

Do not run a full `./vicegerent install` before restoration. It would create empty PVCs that Velero then skips, silently preventing data restoration.

```bash
BACKUP=velero-vicegerent-daily-20260725101005
RESTORE="full-restore-$(date +%Y%m%d%H%M)"

# Recreate only the base cluster and restore prerequisites.
kind delete clusters vicegerent
./vicegerent setup cluster
./vicegerent setup secrets platform
./vicegerent install --stage crds
./vicegerent install --stage storage
./vicegerent install --stage controllers

# Stop if vicegerent is not Available or the backup is not listed.
velero backup-location get
velero backup get

velero restore create "$RESTORE" --from-backup "$BACKUP" --wait
velero restore describe "$RESTORE" --details
kubectl -n velero get datadownloads
kubectl get pods -A

# Re-assert every stage and expected chart version only after restoration.
./vicegerent install
helm list -A
```

If the backup location is `Unavailable` or backups are absent, stop. Start rclone with `./vicegerent start` and check the [`/etc/hosts` fix](#etchosts-must-resolve-hostdockerinternal).

## Gotchas

### `/etc/hosts` must resolve `host.docker.internal`

| | Detail |
|---|---|
| Symptom | `velero backup describe --details`, `velero backup logs`, or `velero restore describe --details` hangs or fails against `host.docker.internal:9899`, while backups still complete. |
| Cause | The BSL uses `s3Url: http://host.docker.internal:9899`. It resolves in-cluster, but these CLI commands fetch from the laptop, where macOS does not resolve it. This affects inspection, not server-side backups. |
| Fix | Map the name to the loopback address where rclone listens. |

```bash
echo '127.0.0.1 host.docker.internal' | sudo tee -a /etc/hosts
ping -c1 host.docker.internal
curl -sS -o /dev/null -w '%{http_code}\n' http://host.docker.internal:9899
```

Expect `ping` to resolve `127.0.0.1` and `curl` to return `403`: rclone answered and rejected the unsigned request. Connection refused means rclone is not running; start it with `./vicegerent start`.

### Agent volumes survive Sandbox deletion

Agent PVCs are chart-owned instead of Sandbox `volumeClaimTemplates`. Otherwise the sandbox controller would own the claims, and `kubectl delete sandbox` would garbage-collect all three; the `Delete` reclaim policy would then remove their data. The claims also use `helm.sh/resource-policy: keep` so `helm uninstall` preserves them.

Chart ownership also reasserts labels on every Helm upgrade. Claim-template metadata is read only at creation, so an exclusion label added later would not reach an existing PVC and could disappear when a restore recreates the claim from an older backup.
