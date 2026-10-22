# CNPG handoff for the first Piraeus node

Read-only handoff prepared 2026-10-21. Maintenance is authorized; none of the
operations below has been executed at this checkpoint.

## Observed state

Read-only live inspection found the CNPG operator image at `1.30.0` and the
installed `kubectl cnpg` client at `1.30.1`. The Flux owners
`ducktape-flux/nix-cache` and `ducktape-flux/study-casino` were `Ready=True`,
`Suspended=False` at inspection; both must continue to own their Cluster
resources throughout this procedure.

| Cluster                        | Current members                        | Target member                                                                                                             | Off-target members                                                                   |
| ------------------------------ | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `nix-cache/attic-db`           | 2/2 ready; primary `attic-db-1`        | `attic-db-1` on `ovh-ns1001419`; 2 GiB `local-path-ovh-ssd`, PV `pvc-5973cabc-c6a0-4d8e-b2a1-f2fcdafa1db3`                | `attic-db-2` on `ovh-ns104952`                                                       |
| `study-casino/study-casino-db` | 3/3 ready; primary `study-casino-db-6` | replica `study-casino-db-2` on `ovh-ns1001419`; 1 GiB `local-path-ovh-ssd`, PV `pvc-3c5bf403-1bfc-45bf-a208-443919aa1703` | `study-casino-db-1` on `ovh-ns104963`; primary `study-casino-db-6` on `ovh-ns104952` |

Both target PVs had reclaim policy `Delete` at inspection. Their old data
volumes must be retained before CNPG removes their claims. Read-only checks found
`.spec.maxSyncReplicas` evaluates to `0` for both Clusters, so the controller's
sync-replica floor does not block the proposed counts; recheck before execution.
The target Attic filesystem directory was measured at 3.3 decimal GB against a
2 GiB claim; the Study Casino directory was 620 MB against 1 GiB. Recheck PostgreSQL data, WAL,
peak/temp use and free space before choosing replacement sizes; 8 GiB (Attic)
and 4 GiB (Study Casino) are initial candidates, not verified requirements.

Both Clusters are generated: Attic from `cluster/cdk8s/nix_cache/attic.py`
and Study Casino from `cluster/cdk8s/study_casino/app.py`. Change those sources
and regenerate their manifests for the final desired state. Keep the existing
Flux owners. A short, recorded suspension of the two owning Kustomizations can
hold the reviewed maintenance phases; resume them against matching committed
configuration. No global Flux or CNPG operator shutdown is required. The live/repo Piraeus class
`local-canary` is constrained to the canary pool and is not the target class.

## Handoff sequence

1. **Gate the Piraeus destination.** Before either database is restored, deploy
   and verify a production strict-local StorageClass backed by an SSD pool
   available on `ovh-ns1001419`. Require `volumeBindingMode: WaitForFirstConsumer`,
   single-node placement and remote access disabled. Verify the target's
   Piraeus satellite, pool capacity, and CSI provisioner are healthy after the
   node returns. Do not use `local-canary`, edit a shared `local-path` class, or
   statically pre-create a CNPG PVC. CNPG's dynamic PVC creation lets the Pod's
   required anti-affinity select the target host before Piraeus provisions its
   volume.

2. **Prove the remaining data path and protect the old sources.** Refresh member
   readiness, primary identity, replication lag, latest base-backup completion
   and WAL archive health. Record each target PVC's PV name and UID, node and
   volume identity. Recheck the PV UID and patch only these two target PVs to
   `persistentVolumeReclaimPolicy: Retain` before lowering either Cluster's
   desired instance count. Do not change the `local-path` StorageClass. Continue
   only after the patch is observed on the same PV UIDs. `Retain` protects the
   backing volume from PVC reclaim; it cannot protect data from a later disk
   reformat, so prove off-node recovery before any conversion that can overwrite
   the source disk.

3. **Retire Study Casino's target replica declaratively.** Recheck that member
   `-2` is still a ready, active replica on `ovh-ns1001419`, primary `-6` is on
   `ovh-ns104952`, and replica `-1` is ready on `ovh-ns104963`. Set the
   Cluster's reviewed maintenance phase to `instances: 2`, preserving its required hostname
   anti-affinity and storage settings. CNPG 1.30.0's downscale selector protects
   the primary and removes the highest-serial ready replica, so under those
   verified conditions it selects target member `-2`. Wait for that Pod and
   PVC to be gone, the retained source PV to be `Released` and still present,
   and members `-1` and `-6` to remain healthy on their original nodes. Desired
   count 2 prevents CNPG from replacing the removed member; do not run
   `kubectl cnpg destroy` while `instances: 3` is desired. No anti-affinity
   relaxation or operator pause is needed. Keep count 2 through target
   maintenance.

4. **Evacuate Attic while retaining two off-target copies.** First remeasure
   PostgreSQL data/WAL and verify capacity on `ovh-ns104963`; select a size with
   headroom (8 GiB is the initial candidate). Set the evidence-based
   `storage.size` together with `storage.resizeInUseVolumes: false`, keeping
   `instances: 2` and the current storage class. Observe that
   template on the live Cluster before scaling up. This leaves both 2 GiB existing
   claims untouched while a new member uses the larger claim. Then set
   `instances: 3` while `attic-db-1` is still on the target. Required hostname
   anti-affinity should place new member `attic-db-3` on the only unoccupied
   survivor, `ovh-ns104963`; verify its node, Ready state, streaming lag and
   healthy PVC before continuing. Confirm `attic-db-2` is a healthy, caught-up
   standby on `ovh-ns104952`, then use
   `kubectl cnpg promote attic-db 2 --namespace nix-cache` and wait until `-2`
   is primary and `-1` (target) and `-3` (104963) are healthy standbys.

   CNPG 1.30.0 supports the per-Cluster annotation
   `cnpg.io/reconciliationLoop: "disabled"`; upstream documents that it
   disables all Cluster reconciliation, including self-healing and failover.
   Keep this as a short, supervised hold. Set the annotation while desired
   instances remains 3 and observe it on the live Cluster before setting
   `instances: 2`. This ordering prevents the normal downscale selector from
   choosing higher-serial standby `-3` instead of target member `-1`. Use the
   recorded scoped Flux hold to keep these intermediate fields from reverting.

   With the operator held and the target PV already set to `Retain`, run the
   installed, version-verified plugin command:
   `kubectl cnpg destroy attic-db 1 --keep-pvc --namespace nix-cache`. In CNPG
   plugin 1.30.1, `--keep-pvc` detaches the claim from the Cluster, marks it
   detached, then deletes the exact member Pod; it does not delete the PVC. Verify
   `attic-db-1` is gone, its detached PVC remains bound to the recorded retained
   PV, and `attic-db-2` primary plus `attic-db-3` standby remain healthy on
   separate survivor nodes. Resume CNPG by removing the annotation; keep `instances: 2`
   through the node rebuild. Verify
   the same two off-target members remain healthy and the detached target claim
   is still present. Do not leave the reconciliation-disable annotation in place:
   while set, the Cluster cannot self-heal or fail over.

5. **Prove off-node recovery before conversion.** Confirm the Study Casino
   target claim has been deleted with its source PV retained, and Attic's
   detached target PVC/PV and backing source data are still intact. Record the fresh off-node rebuild of Attic member 3, its successful
   catch-up to the promoted primary and database access through that member.
   Verify Study Casino's two surviving members are caught up and usable on
   separate nodes. These application-native copies are the maintenance recovery
   path; an additional etcd snapshot or S3 restore is not a prerequisite. If that proof
   fails, stop before touching the source disk. Once it passes, remove Attic's
   detached old claim with the supported
   `kubectl cnpg destroy attic-db 1 --namespace nix-cache` command; this deletes
   the detached PVC, while its `Retain` PV/backing data remains. Confirm the PV
   is `Released` and still present. Only then may the node maintenance procedure
   convert/reformat the source disk. Retain the old PV objects until the new
   Piraeus members are healthy and recovery is again demonstrated.

6. **Restore one new member per Cluster on Piraeus.** After the target is Ready,
   its production Piraeus pool is healthy, and backup/WAL plus surviving-member
   health are rechecked, update each existing Cluster source to its verified
   new `storageClass`, evidence-based size (initial candidates: Attic 8 GiB,
   Study Casino 4 GiB), and `storage.resizeInUseVolumes: false`. In the committed final
   desired state, raise Attic from 2 to 3 and Study Casino from 2 to 3. Keep Study
   Casino's required hostname anti-affinity unchanged. CNPG will make new
   dynamically provisioned claims using the current Cluster storage template;
   `WaitForFirstConsumer` binds the strict-local Piraeus volume with the Pod on
   the only host permitted by anti-affinity. Wait for each new Pod/PVC to be
   Ready, confirm its node is `ovh-ns1001419`, its new PV uses the production
   Piraeus class and strict-local pool, and all pre-existing sibling PVCs retain
   their original class and size. `resizeInUseVolumes: false` is the documented
   guard that prevents the new size from expanding the siblings' existing PVCs.

7. **Close only after recovery is demonstrated.** Verify both Clusters' desired
   member counts and health, primary roles, target-local placement, new PVC/PV
   identities and successful archive/backup progression. Retain the old source
   PVs until the off-node database recovery path has been proved to the operator's
   acceptance criteria. Only then consider separately authorized cleanup.

CNPG 1.30 assigns the lowest free instance serial, so the retired Study member
will normally be recreated as `study-casino-db-2` and Attic as `attic-db-1`.
For Attic, remove the old detached PVC only after off-node recovery proof and
before scaling up; otherwise that claim name would obstruct or be mistaken for
the new member's claim. Treat serial reuse as an expectation, not a command
target: inspect the actual Pod, PVC and owner labels after scaling up before
evaluating its new storage.

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
- If a downscale event proposes a member other than the verified Study target,
  stop before proceeding. The selection depends on current active/ready state,
  primary role and serial. The Attic path deliberately holds reconciliation
  instead of relying on downscale selection. The nix-cache Flux Kustomization
  uses the repository helper's `wait: true` default. While the CNPG annotation is
  set and `instances` is changed, verify Flux's applied revision and the live
  Cluster fields directly; the CNPG Ready status may be stale. After removing the
  annotation, confirm CNPG health and Flux readiness recover. Resume any scoped Flux holds against the final committed desired state;
  preserve the existing owners.

## Upstream references

- [CNPG 1.30.0 storage: resizing, re-creating storage, and dynamic provisioning](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/docs/src/storage.md#L284-L358)
- [CNPG 1.30.0 scale-down controller](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/cluster_scale.go#L40-L72) and [exact member selector](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/replicas.go#L464-L500)
- [CNPG 1.30.0 release notes: lowest-free serial reuse](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/docs/src/release_notes/v1.30.md#L349-L357)
- [CNPG 1.30.0 source: per-Cluster reconciliation-disable annotation](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/cluster_controller.go#L310-L316) and [CNPG docs: its self-healing/failover implications](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/failure_modes.md#L61-L91)
- [CNPG 1.30.1 `destroy --keep-pvc` implementation](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/internal/cmd/plugin/destroy/destroy.go#L39-L95) and [1.30.1 release note for PVC/Pod ordering fix](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/release_notes/v1.30.md#L239-L245)
- [Kubernetes PV reclaim policies](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#reclaiming)
- [Kubernetes StorageClass binding mode](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode)
