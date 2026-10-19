# DRBD canary execution

Disposable fixtures for the [canary runbook](../../docs/drbd_canary.md).
These resources are applied explicitly for the experiment; Flux does not own
them. The provider itself is managed through Flux. Do not run before both
workers and the `local-canary` class pass preparation checks.

Live observations are in [results.md](results.md). Review the
[FILE_THIN resync investigation](thin_resync.md) before repeating relocation:
the first sparse filesystem resync stalled with the generated defaults.

The separate [auto-diskful experiment](auto_diskful/README.md) evaluates automatic
local copying and old-copy cleanup after moving only the consuming Pod.

`config_paths_bin` compares private multi-document machine configurations and
prints changed paths without their values. `--canary-output PATH` writes a
configuration preserving the first input's live settings while copying only
the installer image, kernel modules, and canary label from the second input.
Keep inputs and output in a private directory, then compare the output against
the live input again before use.

```bash
bb run //cluster/debug/drbd_canary:config_paths_bin -- BEFORE.yaml AFTER.yaml
```

```bash
kubectl apply -k cluster/debug/drbd_canary
kubectl -n drbd-canary-tests wait pod/block pod/sparse pod/filesystem --for=condition=Ready --timeout=180s
kubectl -n drbd-canary-tests exec block -- fio --version
kubectl -n drbd-canary-tests exec sparse -- fio --version
kubectl -n drbd-canary-tests exec filesystem -- fio --version
```

All three Pods prefer `ovh-ns102453` but can run on either prepared canary
worker. Record their actual nodes, PVC UIDs, PV names, and CSI handles before
testing. The `block` claim is 1Gi and is the raw-volume migration, fill, and
resize target; the `filesystem` claim is 4Gi and remains the filesystem-mode
target.

The separate `sparse` claim is a 50Gi raw Block volume for sparse creation and a
small write probe only. Its `seed` and `verify` exercises touch only the first
64Mi; the exercise script rejects `fsync` and `stream` for this target. Do not
move this volume during the initial migration tests: a full initial
resynchronization could allocate more than the 20Gi per-node physical budget.
Do not attempt a 50Gi resync until sparse-resynchronization behavior has been
established and its physical-allocation budget explicitly reviewed. Monitor the
shared filesystems throughout and stop before the documented budget or existing
operational headroom is exhausted. Runtime package versions are logged by `apk`;
capture them alongside fio output.

```bash
kubectl -n drbd-canary-tests exec block -- sh /exercises/exercise.sh seed /dev/canary
kubectl -n drbd-canary-tests exec sparse -- sh /exercises/exercise.sh seed /dev/sparse
kubectl -n drbd-canary-tests exec filesystem -- sh /exercises/exercise.sh seed /data/payload.bin
kubectl -n drbd-canary-tests exec block -- sh /exercises/exercise.sh fsync /dev/canary
kubectl -n drbd-canary-tests exec filesystem -- sh /exercises/exercise.sh fsync /data/payload.bin
kubectl -n drbd-canary-tests exec sparse -- sh /exercises/exercise.sh verify /dev/sparse
```

`seed` writes and reads back CRC-protected blocks. `fsync` measures synchronous
random writes for 20 seconds with fio's latency reporting. `stream` runs for up
to five minutes, reporting every five seconds, for controlled synchronization
and failure experiments. Save the output outside the Pods before recreating
them. `verify` reads the CRC-protected region without rewriting it; run it after
each movement, detach/reattach, or recovery.

The scoped migration and fault-injection commands require resolving the exact
LINSTOR resource and reviewing its current roles/peers first. Move each claim's
backing storage separately, then recreate only its Pod after cutover. Keep the
PVC and verify its UID and data. Do not apply a cluster-wide network policy,
stop unrelated node services, or manipulate another volume's connections.

The class retains released PVs. Capture their CSI handles before any teardown;
deleting these PVCs or the test namespace does not clean up the retained backing
data. Remove only the recorded disposable resources after verification.
