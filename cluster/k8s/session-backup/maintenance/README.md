# Remove the historical Codex thread-history projection

This is operator-run maintenance; merging it does not modify the cluster or backups.
The tool stages replacements without forgetting originals and checks each replacement
with `restic diff`. It targets `thread_history_1.sqlite` and its `-wal`, `-shm`, and
`-journal` sidecars at `/.codex/` and in the standard staged
`/.local/state/ducktape/ai-session-backup-sqlite/images/codex/<root-id>/` directory.
It does not remove other databases or old caches.

The server is append-only, so pruning needs separate maintenance access. The repository
has two cluster storage replicas but no independent recovery copy. Make and verify an
independent Restic repository copy before removing originals. Rewriting changes
snapshot IDs and retains old `manifest.json` files that may still list the removed image.
The manifest's other images remain useful, but its thread-history entry must not be used
for restoration.

## Before starting

1. Confirm the new exclusion is deployed and working on each workstation; merging its
   source PR alone does not update their Home Manager configurations. Otherwise future
   snapshots can reintroduce the projection.
2. Make and verify an independent, recoverable copy of each repository to be changed.
   Record the repository ID, snapshot inventory, free space, and `restic check` result.
   Pruning may need temporary space and blocks repository operations. Preserve the plan,
   preview, and verification report outside the repository being maintained.
3. Coordinate a maintenance window: stop workstation backup timers and suspend the
   three `ai-session-archive-import-*` CronJobs, then allow active import and backup
   jobs to finish. Do not delete a running importer to make the window. Record the prior
   timer and CronJob states so they can be restored. The importer retains an internal
   ledger of old IDs and discovers rewritten snapshots as new; it may need a long
   re-import and its database-only counts retain old snapshot history.
4. On the operator machine, use the repo devshell's `restic`, `python`, `sops`, and
   `kubectl`. Set `umask 077`, disable shell tracing, and use a private scratch directory
   for the plan and logs. Select one host at a time (`wyrm2`, `rugged`, or `iguana`).
   Credentials come from `secrets/shared/session-backup.yaml`; keep them out of logs,
   command-line arguments, and committed files.

## Access the repository

The template launches a second, authenticated Rest Server on the same node as the active
server, mounting the existing RWO PVC. It listens only on the pod's loopback interface;
no Service or route points at it. It intentionally omits `--append-only`. Its existence
must be temporary and restricted to the operator's maintenance window. Review the live
deployment, node, PVC, image, and network policy before creating it; do not use this
template if those no longer match. Do not suspend Flux or modify the normal server.

```sh
export MAINTENANCE_NODE="$(kubectl -n session-backup get pod -l app.kubernetes.io/name=session-backup -o jsonpath='{.items[0].spec.nodeName}')"
envsubst '$MAINTENANCE_NODE' < cluster/k8s/session-backup/maintenance/rest-server.yaml | kubectl apply -f -
kubectl -n session-backup wait --for=condition=Ready pod/session-backup-maintenance --timeout=120s
kubectl -n session-backup port-forward --address 127.0.0.1 pod/session-backup-maintenance 18000:8000
```

Run the port-forward in a separate terminal and verify the target pod and endpoint. In
the operator shell, set the selected host and read credentials directly into environment
variables (do not enable `set -x`):

```sh
backup_host=wyrm2
export RESTIC_REPOSITORY="rest:http://127.0.0.1:18000/$backup_host"
export RESTIC_REST_USERNAME="$(sops -d --extract "[\"restic_rest_username_$backup_host\"]" secrets/shared/session-backup.yaml)"
export RESTIC_REST_PASSWORD="$(sops -d --extract "[\"restic_rest_password_$backup_host\"]" secrets/shared/session-backup.yaml)"
export RESTIC_PASSWORD="$(sops -d --extract '["restic_password"]' secrets/shared/session-backup.yaml)"
restic snapshots
restic check
```

For example, with an empty independent storage location mounted at
`$independent_root` and enough free space, create a separate repository per host.
This copies every retained snapshot and can require substantial time and bandwidth:

```sh
export RESTIC_FROM_REPOSITORY="$RESTIC_REPOSITORY"
export RESTIC_FROM_PASSWORD="$RESTIC_PASSWORD"
export RESTIC_REPOSITORY="$independent_root/$backup_host"
restic init
restic copy
restic check --read-data
restic snapshots
export RESTIC_REPOSITORY="$RESTIC_FROM_REPOSITORY"
unset RESTIC_FROM_REPOSITORY RESTIC_FROM_PASSWORD
```

Compare the copied snapshot inventory with the source and restore samples from the
copy before proceeding. Confirm the independent destination is outside the cluster
failure domain, remains available for recovery, and is not the source PVC. If using
an existing copy instead of a new repository, first verify it and update it with
`restic copy`; do not reinitialize it. Keep the encryption password available for
recovery. Snapshot IDs in the copy differ from source IDs.

## Preview, stage, verify, then remove

Use a fresh private working directory outside the repository and set `$maintenance_dir`
to it. The preview reports every excluded path; check that all affected raw and staged
paths match the intended projection. Nonstandard `CODEX_HOME` or XDG paths are outside
the tool's patterns: inspect snapshot inventories for additional copies before deciding
the scope is complete. The tool records full snapshot IDs and repository identity, and
the stage may be rerun after interruption; it keeps all original snapshots. A changed
repository identity or missing original stops it.

```sh
python cluster/k8s/session-backup/rewrite_thread_history.py plan "$maintenance_dir/plan.json" | tee "$maintenance_dir/preview.txt"
python cluster/k8s/session-backup/rewrite_thread_history.py stage "$maintenance_dir/plan.json" | tee "$maintenance_dir/stage.txt"
python cluster/k8s/session-backup/rewrite_thread_history.py verify "$maintenance_dir/plan.json" "$maintenance_dir/verified.json"
```

Verification checks each replacement's identity, selected snapshot metadata and tags,
and diff: every reported change must be deletion of a targeted projection file. It
fails on duplicate replacements or unexpected changes; it does not claim to validate
every byte of every file. Originals that did not produce a replacement remain untouched.
Before forgetting anything, compare the report against the preview, inspect representative
rewritten snapshots and manifests, restore representative journals and memory images,
and confirm the independent recovery copy. Resolve any mismatch first. Existing readers
must remain quiesced throughout removal and pruning.

Only after that review, forget **exactly** the report's original IDs, then run
`restic prune --dry-run` and review its space estimate before `restic prune`. Run
`restic check` afterwards and verify representative restores again. Do not use a
retention policy or `rewrite --forget`: either could remove data without this review.
The report can drive the explicit forget command after review:

```sh
jq -r '.replacements | keys[]' "$maintenance_dir/verified.json" | xargs -r restic forget
restic prune --dry-run
# Review the output, then run restic prune and restic check.
```

On interruption, keep originals and the plan; do not remove locks blindly. Stop the
port-forward, delete the maintenance pod, restore the recorded timer and CronJob states,
and verify the normal append-only server and imports work. Repeat for each repository.
Track unprocessed snapshots explicitly; logical file size does not equal space reclaimed
because Restic shares chunks across files and snapshots.
