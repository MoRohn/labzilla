# Disaster recovery

## State and where it lives

| Data | Location | Backed up | Loss impact |
|---|---|---|---|
| Model registry, aliases, activity, availability (`registry.db`) | PVC `ai-system/lif-registry` (Longhorn) | **yes**: Longhorn `backup-daily` RecurringJob, 03:15, 14 kept (first backup and restore drill 2026-10-01) | High: alias versions and evidence. The seed rebuilds a minimal registry from `config/models.yaml` |
| Decision log (`decisions.db`) and Decision Engineering (`decision-eng.db`, `calibration/`) | `lif-decisions` (**local-path**, so Longhorn can't back it up) | **yes**: CronJob `lif-sqlite-backup`, 03:45, consistent SQLite copies to `s3://longhorn-backups/lif-sqlite/<date>/`, 14 days (first run and restore check 2026-10-01) | Decision log: low (30-day history). `decision-eng.db`: high (releases, provenance, shadow and outcome data) |
| Batch queue (`batch.db`) | `lif-batch` (Longhorn) | **yes**: Longhorn `backup-daily` | Medium: pending jobs |
| Model weights | `ai-serving/model-store` (local-path, a host dir) | **no**, re-downloadable | Re-download by sha-pinned Job |
| Images | `ai-system/registry` (local-path) | no | Rebuild with `docker build` |
| Config | git (`config/`, manifests) | git | none |
| Secrets | `labzilla/.env.local`, `secrets/lif-*.key` (host only) | **no**: back these up out of band | Regenerate keys with `create-secrets.sh` (clients must update) |

## Reboot (designed order)

1. K3s starts.
2. Model servers start: tier0, tier0-small and embedding, with startup probes up to 300 s.
3. Gateway: ready only when at least one model is healthy (`/readyz`).
4. Controller: its probes run every 30 s. gpusched is unreachable until its user unit starts, and LIF treats that as **IMMINENT (fail-safe)** until it answers.
5. Decision fabric and batch: batch items that were `running` return to `pending`.
6. gpusched's user unit may start before or after K3s. Until LIF can read it, the state stays IMMINENT, so CPU tiers run at concurrency 1 (cluster-wide) and batch is paused.

"Container started" is not "usable": readiness and the availability probes check real inference.

**Status: a host reboot has not been exercised.** It is pending owner approval, because it reboots primary-workload production too.

## Failure scenarios

| Failure | Detect | Contain / recover | Validated |
|---|---|---|---|
| Model server crash | `/health` probe (10 s) + passive breaker | Router falls back along the alias chain (`fallback: true`); kubelet restarts the pod | unit test (gateway fallback) |
| Gateway pod crash | readiness | Second replica + PDB | – |
| Controller down | – | Gateway keeps its last-known-good routing table; probes and guard pause | design |
| Jev outage / no internet | breaker, `LIFJevCircuitOpen` | Rules answer. Installed models unaffected. Discovery and downloads fail cleanly | unit test (502 → rules) |
| HF outage | discovery run `failed` with error | Nothing else affected | – |
| gpusched down | metrics unreachable | State = IMMINENT: minimum CPU concurrency, batch paused | unit test |
| Host memory pressure | guard (MemAvailable via gpusched) | Shed `tier0-small`, then `embedding` | **live 2026-10-01 07:21** |
| Corrupt download | sha256 mismatch | `*.bad`, state QUARANTINED | code path (not exercised live) |
| Bad promotion | quality drop / fallbacks | `local-ai models rollback <alias>` | unit test (registry rollback) |
| Disk pressure | – | Retention GC (7 days); 3.3 TB free today | – |

## Restore procedures

**Re-seed the model store:**

```bash
kubectl apply -f deploy/k8s/bootstrap/download-tier0.yaml
```

Cached files are re-verified rather than re-downloaded.

**Restore `registry.db` or `batch.db`** (Longhorn backup):

1. Restore the Longhorn backup of `lif-registry` from MinIO (Longhorn UI → Backup → restore to a new volume).
2. Scale the controller to 0 and swap the PVC.
3. Scale the controller back to 1.

**Restore `decisions.db` / `decision-eng.db`** (SQLite backup):

1. Check the latest set first (download, sha256, `integrity_check`). Credentials come from `secrets/lif-backup-minio.env`:

   ```bash
   (set -a; . ../secrets/lif-backup-minio.env; set +a
    S3_ENDPOINT=$LIF_BACKUP_MINIO_ENDPOINT S3_ACCESS_KEY=$LIF_BACKUP_MINIO_USER S3_SECRET_KEY=$LIF_BACKUP_MINIO_PASSWORD \
    .venv/bin/python scripts/sqlite_backup.py verify-latest)
   ```
2. `kubectl -n ai-system scale deploy/decision-fabric --replicas=0`.
3. Download `lif-sqlite/<date>/<name>.db.gz` (for example with `mc` in the `bnn-minio` container), gunzip it, and copy it into the `lif-decisions` volume with a short-lived pod that mounts the PVC. Delete any `<name>.db-wal` and `<name>.db-shm` left beside it.
4. Scale decision-fabric back to 1. Run `local-ai decision registry` to confirm the releases are back.

The `lif-backup` MinIO user can only read and write `lif-sqlite/`, not Longhorn's backups (403, checked 2026-10-01). The off-site sync mirrors the whole bucket, so these copies leave the host once off-site is set up.

**Lost secrets:**

1. Delete `secrets/lif-*.key` and run `scripts/create-secrets.sh`.
2. `kubectl -n ai-system rollout restart deploy`.
3. Hand out the new keys (an optional external workload uses `lif-workload.key` when that integration is on).
