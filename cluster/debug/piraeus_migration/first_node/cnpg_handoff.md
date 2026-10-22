# CNPG handoff for the first Piraeus node

Off-node application handoffs completed 2026-10-22. This record distinguishes
the completed move to existing survivor storage from the deferred Piraeus
restore. No Piraeus application PVC has been created and no app data has been
restored to Piraeus. The explicit user pause before that work remains in force.

## Initial preflight snapshot (2026-10-21)

This is the read-only baseline before the handoffs below; it is not current
placement state.

Read-only live inspection found the CNPG operator image at `1.30.0` and the
installed `kubectl cnpg` client at `1.30.1`. The Flux owners
`ducktape-flux/nix-cache` and `ducktape-flux/study-casino` were `Ready=True`,
`Suspended=False` at inspection; both must continue to own their Cluster
resources throughout this procedure.

| Cluster                        | Members at snapshot                    | Target member                                                                                                             | Off-target members                                                                   |
| ------------------------------ | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `nix-cache/attic-db`           | 2/2 ready; primary `attic-db-1`        | `attic-db-1` on `ovh-ns1001419`; 2 GiB `local-path-ovh-ssd`, PV `pvc-5973cabc-c6a0-4d8e-b2a1-f2fcdafa1db3`                | `attic-db-2` on `ovh-ns104952`                                                       |
| `study-casino/study-casino-db` | 3/3 ready; primary `study-casino-db-6` | replica `study-casino-db-2` on `ovh-ns1001419`; 1 GiB `local-path-ovh-ssd`, PV `pvc-3c5bf403-1bfc-45bf-a208-443919aa1703` | `study-casino-db-1` on `ovh-ns104963`; primary `study-casino-db-6` on `ovh-ns104952` |

Both target PVs had reclaim policy `Delete` at this snapshot. Before removing
their claims, both were changed to `Retain` after exact PV/claim UID checks.
`.spec.maxSyncReplicas` evaluated to `0` for both Clusters, so the controller's
sync-replica floor did not block reducing them to two instances. The target
Attic directory measured 3.3 decimal GB against a 2 GiB claim; Study Casino
measured 620 MB against 1 GiB. The completed new Attic replica uses 8 GiB with
`resizeInUseVolumes: false`; existing 2 GiB claims were unchanged. Study Casino
keeps its existing 1 GiB instance claims.

Both Clusters are generated: Attic from `cluster/cdk8s/nix_cache/attic.py` and
Study Casino from `cluster/cdk8s/study_casino/app.py`. Keep their existing Flux
owners. A child Kustomization suspension was attempted during the Attic
handoff, but its parent reverted it and the first clone briefly scaled down;
the original two members remained healthy. The completed handoff instead used
the documented per-resource `kustomize.toolkit.fluxcd.io/reconcile: disabled`
annotation on the Cluster and removed it after the transition. No such holds
remain. No global Flux or CNPG operator shutdown is needed. The live/repo
Piraeus class `local-canary` remains constrained to the canary pool and is not
the production target class.

## Completed off-node handoff (2026-10-22)

- **Study Casino:** target member `study-casino-db-2` and its claim were removed
  after confirming the two off-target members. The Cluster is now 2/2 ready:
  primary `study-casino-db-6` on `ovh-ns104952`, standby
  `study-casino-db-1` on `ovh-ns104963`, zero replication lag. The released
  target PV remains `Retain`; remaining instance claims are still 1 GiB.
- **Attic:** updated only the new-claim template to 8 GiB with
  `resizeInUseVolumes: false`; existing 2 GiB claims were unchanged. Fresh
  member `attic-db-3` cloned to `ovh-ns104963`, read successfully, and caught up
  with zero lag. After promotion of `attic-db-2` on `ovh-ns104952`, member 3 is
  the healthy standby. The old target claim was retired only after this proof;
  its source PV remains `Released` with `Retain`.
- Both Clusters are 2/2 ready on separate off-target nodes. The old target PVs
  were protected by exact PV/claim UID checks, and all temporary Flux holds have
  been removed. See the [sanitized handoff proof](cnpg_offnode_handoff_proof.json).

The target rebuild and cold reboot have completed; it remains cordoned. The
four original target PV objects are `Released`/`Retain` inventory records, but
the approved data-disk wipe erased their backing files. The recovery sources
are the verified off-node copies. No application Piraeus PVC has been created;
see the [physical-node checkpoint](README.md#physical-node-checkpoint-2026-10-22).
The user pause before repopulating application storage remains in force.

## Handoff sequence

Steps 2–5 completed on existing off-node storage. Piraeus restoration remains
paused; no application PVC may be created until node/pool acceptance and the
user's explicit approval.

1. **Deferred: gate the Piraeus destination.** After first-node acceptance and
   explicit approval, deploy and verify a production strict-local StorageClass
   backed by an SSD pool available on `ovh-ns1001419`. Require
   `volumeBindingMode: WaitForFirstConsumer`, single-node placement and remote
   access disabled. Verify the target satellite, pool capacity, protected data
   and metadata alerts, and CSI provisioner after the node returns. Do not use
   `local-canary`, edit a shared `local-path` class, or statically pre-create a
   CNPG PVC. Both this gate and user approval must pass before the first
   production Piraeus PVC is created.

2. **Complete: protect the original CNPG sources.** Both target PVs were set to
   `Retain` and read back after exact PV/claim UID checks before their claims
   were removed. The Study Casino and Attic target PVs remain `Released` with
   `Retain`; the Attic PV name and UID are in the
   [handoff proof](cnpg_offnode_handoff_proof.json). Retain protects a backing
   volume from PVC reclaim, not from disk reformat.

3. **Complete: evacuate Study Casino's target replica.** Member `-2` and its
   claim were removed after verifying the two surviving members. Member `-6`
   remains primary on `ovh-ns104952`; member `-1` is the healthy standby on
   `ovh-ns104963`. The Cluster is 2/2 ready with zero lag, the source PV is
   `Released`/`Retain`, and remaining instance claims are 1 GiB.

4. **Complete: evacuate Attic while retaining two off-target copies.** A fresh
   8 GiB member 3 was created on `ovh-ns104963` with
   `resizeInUseVolumes: false`; both existing 2 GiB claims were unchanged. After
   clone read and catch-up, member 2 on `ovh-ns104952` was promoted and member 3
   became its healthy standby. The old target claim was detached and removed
   only after recovery proof; its source PV remains `Released`/`Retain`. Attic
   is 2/2 ready with zero lag.

   A direct suspend of the child `nix-cache` Kustomization was reverted by its
   parent and briefly scaled the first clone back down; the original two
   members stayed healthy. The successful hold used the documented per-resource
   `kustomize.toolkit.fluxcd.io/reconcile: disabled` annotation on the Attic
   Cluster, then removed it. Do not repeat the child-Kustomization suspend
   approach. Keep any resource-level hold short because it disables CNPG
   reconciliation, self-healing and failover.

5. **Complete: prove off-node recovery.** Both Clusters are healthy at count 2
   on separate nodes and have zero lag. The handoff proof records the retained
   Attic PV identity, primary/standby placement, and unchanged Study Casino
   1 GiB claims. This completes the data-preservation gate only. Node-level
   maintenance began after this checkpoint and remains in progress; this does
   not authorize Piraeus PVC creation.

6. **Deferred: restore one new member per Cluster on Piraeus.** Only after the
   target is Ready, its production Piraeus pool and data/metadata alert delivery
   pass acceptance, and the user authorizes this restore, recheck backups/WAL and
   surviving-member health. Update each existing Cluster source to its verified
   production `storageClass`, agreed size (Attic 8 GiB, Study Casino unchanged
   at 1 GiB), and `storage.resizeInUseVolumes: false`. Raise Attic and Study
   Casino from 2 to 3 while keeping Study Casino's required hostname
   anti-affinity unchanged. CNPG dynamically provisions claims using the
   current Cluster storage template; `WaitForFirstConsumer` binds the strict-
   local Piraeus volume with the Pod on the only host permitted by anti-affinity.
   Verify each new Pod/PVC is Ready on `ovh-ns1001419`, its PV uses the
   production Piraeus class and strict-local pool, and existing sibling PVCs
   retain their class and size. `resizeInUseVolumes: false` prevents the new
   size from expanding sibling claims.

7. **Deferred: close only after recovery is demonstrated.** Verify both
   Clusters' desired counts and health, primary roles, target-local placement,
   new PVC/PV identities and successful archive/backup progression. Retain the
   old source PVs until off-node database recovery has again been demonstrated.
   Only then consider separately authorized cleanup.

CNPG 1.30 assigns the lowest free instance serial, so the retired Study member
is expected to be recreated as `study-casino-db-2` and Attic as `attic-db-1`.
Attic's old detached PVC was removed after off-node recovery proof, freeing its
claim name before the deferred Piraeus restore. Treat serial reuse as an
expectation, not a command target: inspect the actual Pod, PVC and owner labels
after scaling up before evaluating its new storage.

## Controller and storage details

- CNPG documents `resizeInUseVolumes: false` for re-creating storage; while it
  is false, changing the Cluster PVC template's `size` and `storageClass` does
  not resize or mutate extant PVCs. It applies to new claims. Kubernetes PV
  `Retain` preserves the old PV/backing volume after CNPG removes its PVC; the
  PV remains released and requires deliberate later cleanup.
- Storage class is a Cluster-wide creation template, not a per-instance class
  setting. Existing sibling claims can remain on local-path while a newly
  created member gets Piraeus. If the Cluster keeps the Piraeus class as its
  template, any later newly created member also gets Piraeus; reverting the
  template after restoration means future replacement claims use the reverted
  class. Choose and record that steady-state policy. Do not resize siblings as
  part of this handoff.
- The source CRs currently have no dedicated WAL PVC. If that changes before
  execution, identify and retain the matching WAL PV too; the CNPG re-creation
  procedure requires handling both data and WAL claims.
- The Study Casino downscale selected `-2` only after checking the current
  active/ready members, primary and serial. For any future intentional
  downscale, stop if the proposed member differs from the verified target.
- CNPG documents `cnpg.io/reconciliationLoop: "disabled"` as disabling all
  Cluster reconciliation, including self-healing and failover. The completed
  Attic handoff used this as a short hold on the Cluster while changing
  `instances`; a direct child-Kustomization suspend was parent-reverted and
  briefly downscaled the first clone. Use the per-resource Flux reconcile
  annotation on the exact Cluster instead, verify the applied revision and live
  fields directly because CNPG Ready may be stale, then remove the annotation
  promptly. Confirm CNPG health and Flux readiness recover. All temporary holds
  were removed after the completed handoffs; preserve the existing Flux owners.

## Upstream references

- [CNPG 1.30.0 storage: resizing, re-creating storage, and dynamic provisioning](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/docs/src/storage.md#L284-L358)
- [CNPG 1.30.0 scale-down controller](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/cluster_scale.go#L40-L72) and [exact member selector](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/replicas.go#L464-L500)
- [CNPG 1.30.0 release notes: lowest-free serial reuse](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/docs/src/release_notes/v1.30.md#L349-L357)
- [CNPG 1.30.0 source: per-Cluster reconciliation-disable annotation](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/cluster_controller.go#L310-L316) and [CNPG docs: its self-healing/failover implications](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/failure_modes.md#L61-L91)
- [CNPG 1.30.1 `destroy --keep-pvc` implementation](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/internal/cmd/plugin/destroy/destroy.go#L39-L95) and [1.30.1 release note for PVC/Pod ordering fix](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/release_notes/v1.30.md#L239-L245)
- [Kubernetes PV reclaim policies](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#reclaiming)
- [Kubernetes StorageClass binding mode](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode)
