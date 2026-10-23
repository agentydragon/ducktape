# Next Piraeus control-plane preparation: `ovh-ns104952`

Read-only inventory collected on **2026-10-22 around 11:46 UTC** against
`origin/devel` `8a564ab29e`. No workload, PVC, PV, disk, node, or cluster
configuration was changed. This is a dated allocation snapshot, not permission
to drain the node or initialize either NVMe device.

## Node and disk identity

The Kubernetes Node was Ready. Both Piraeus eligibility labels were absent:

| Label                                   | Observed value |
| --------------------------------------- | -------------- |
| `storage.allegedly.works/piraeus-ready` | absent         |
| `storage.allegedly.works/drbd-canary`   | absent         |

Talos read-only disk inventory identified two Intel SSDPE2MX450G7 NVMe devices,
each **450,098,159,616 bytes (419.19 GiB)**:

| Role   | Stable serial        | Current use                                                      | Proposed ownership after separate maintenance                   |
| ------ | -------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------- |
| System | `BTPF8256006P450RGN` | Talos boot/state and current system volumes                      | Talos: ETCD 16, EPHEMERAL 32, CRI 192, KUBELET 128, LOG 16 GiB  |
| Data   | `BTPF8304019P450RGN` | Whole-device XFS UserVolume mounted at `/var/mnt/seaweedfs-data` | Whole device as an LVM PV in VG `piraeus_ssd`, thin pool `data` |

The desired Talos filesystems total **384 GiB**, leaving about **35.19 GiB**
before installer boot partitions, metadata, alignment, and other geometry. The
disk is large enough by nominal arithmetic; actual partition order and fit
still need an approved rehearsal and must be verified before activation. The
data disk is occupied today. Its current XFS UserVolume must remain in place
until its consumers are evacuated and a later maintenance change is approved.

For comparison, the applied first-node LVM_THIN pool used a 512,110,190,592-byte
device, a 4 GiB thin-pool metadata LV, a 4 GiB metadata spare, and 64 KiB
chunks. Its observed thin-data LV was 503,517,806,592 bytes. Applying that same
recipe arithmetically to this target gives at most **441,508,225,024 bytes
(411.19 GiB)** for thin data before PV/LV alignment and extent overhead. This is
an estimate only; LINSTOR has not initialized this disk. The first node's
observed thin-data size was 2,449,408 bytes below its device-minus-8-GiB
arithmetic estimate.

## Bound local PVs and current consumers

All 12 PVs below are Bound, use either `local-path-ovh` or
`local-path-ovh-ssd`, have `hostPath` backing under
`/var/mnt/seaweedfs-data/local-path/`, and are constrained to this node. The
directory suffixes below are the actual per-PV backend paths. Requested sizes
sum to 297 GiB; they are Kubernetes requests, not measured disk usage or a
physical reservation.

| PV                                         | Namespace / PVC                           | Class                | Request | Current consumer role                                       | `hostPath` suffix                                                                          |
| ------------------------------------------ | ----------------------------------------- | -------------------- | ------: | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `pvc-0442a585-0ead-430a-9191-b724e0eab07f` | `atuin/atuin-db-4`                        | `local-path-ovh`     |   2 GiB | CNPG primary; `atuin-db` is 2/2                             | `pvc-0442a585-0ead-430a-9191-b724e0eab07f_atuin_atuin-db-4`                                |
| `pvc-1f2ee994-ef1c-47e2-a28c-363db724be20` | `seaweedfs/seaweedfs-master-1`            | `local-path-ovh-ssd` |   4 GiB | SeaweedFS master StatefulSet member; 3/3                    | `pvc-1f2ee994-ef1c-47e2-a28c-363db724be20_seaweedfs_seaweedfs-master-seaweedfs-master-1`   |
| `pvc-260df6c8-2d87-4e8e-9aef-19a3e8eb5ad6` | `agentplane-staging/postgres-1`           | `local-path-ovh-ssd` |   5 GiB | CNPG standby; `postgres` is 2/2, primary `postgres-2`       | `pvc-260df6c8-2d87-4e8e-9aef-19a3e8eb5ad6_agentplane-staging_postgres-1`                   |
| `pvc-48f4ab5a-f26a-40ca-bdab-289642e0e4ad` | `seaweedfs/mount0-seaweedfs-volume-ssd-1` | `local-path-ovh-ssd` | 250 GiB | SeaweedFS SSD volume-server StatefulSet member; 2/2         | `pvc-48f4ab5a-f26a-40ca-bdab-289642e0e4ad_seaweedfs_mount0-seaweedfs-volume-ssd-1`         |
| `pvc-4f0d6c18-dc14-44e9-a6a5-0f5e1c5e41c3` | `forgejo/forgejo-valkey-ovh-1`            | `local-path-ovh`     |   2 GiB | Forgejo Valkey StatefulSet member; 2/2                      | `pvc-4f0d6c18-dc14-44e9-a6a5-0f5e1c5e41c3_forgejo_forgejo-valkey-ovh-forgejo-valkey-ovh-1` |
| `pvc-59773290-f75e-4fb7-9033-ef517160066d` | `study-casino/study-casino-db-6`          | `local-path-ovh`     |   1 GiB | CNPG primary; `study-casino-db` is 3/3                      | `pvc-59773290-f75e-4fb7-9033-ef517160066d_study-casino_study-casino-db-6`                  |
| `pvc-8333c530-8800-4c81-826d-cfd127a679b5` | `tofu-state/tofu-state-db-ovh-4`          | `local-path-ovh`     |   1 GiB | CNPG primary; `tofu-state-db-ovh` is 2/2                    | `pvc-8333c530-8800-4c81-826d-cfd127a679b5_tofu-state_tofu-state-db-ovh-4`                  |
| `pvc-8467d9ff-7c0b-4fcb-bb8a-c67206649ee1` | `authentik/authentik-db-ovh-4`            | `local-path-ovh`     |   8 GiB | CNPG primary; `authentik-db-ovh` is 2/2                     | `pvc-8467d9ff-7c0b-4fcb-bb8a-c67206649ee1_authentik_authentik-db-ovh-4`                    |
| `pvc-d1cc6f61-3e8a-4b4a-b499-a898eb58609d` | `langfuse/langfuse-db-2`                  | `local-path-ovh-ssd` |  10 GiB | CNPG standby; `langfuse-db` is 2/2, primary `langfuse-db-1` | `pvc-d1cc6f61-3e8a-4b4a-b499-a898eb58609d_langfuse_langfuse-db-2`                          |
| `pvc-d8bb36f6-4a5e-4e8d-b1bb-d27dce6285bb` | `seaweedfs/seaweedfs-filer-db-ssd-2`      | `local-path-ovh-ssd` |   2 GiB | CNPG primary; `seaweedfs-filer-db-ssd` is 2/2               | `pvc-d8bb36f6-4a5e-4e8d-b1bb-d27dce6285bb_seaweedfs_seaweedfs-filer-db-ssd-2`              |
| `pvc-ef623674-9c56-4a55-8f90-544608d295f1` | `nix-cache/attic-db-2`                    | `local-path-ovh-ssd` |   2 GiB | CNPG primary; `attic-db` is 3/3                             | `pvc-ef623674-9c56-4a55-8f90-544608d295f1_nix-cache_attic-db-2`                            |
| `pvc-f38b2a11-c01d-417e-bf5d-3d5744b31edf` | `forgejo/forgejo-db-ssd-2`                | `local-path-ovh-ssd` |  10 GiB | CNPG primary; `forgejo-db-ssd` is 2/2                       | `pvc-f38b2a11-c01d-417e-bf5d-3d5744b31edf_forgejo_forgejo-db-ssd-2`                        |

The seven CNPG clusters whose primary is on this node are Atuin, Authentik,
Forgejo, Attic, SeaweedFS filer, Study Casino, and tofu-state.
Their instance counts and roles above were read from live `Cluster` status and
matched by the target PVC/Pod. The staging PostgreSQL and Langfuse members are
standbys. Forgejo Valkey is an independent StatefulSet-backed queue. The
SeaweedFS master and volume-server claims are control/data-plane state, not
cache. Every item needs its own operator-native handoff or an explicitly
accepted outage before the node can be drained.

## Proposed inert configuration

The review change adds explicit per-node disk serials and separate boolean
gates in `cluster/terraform/main/ovh-nodes.tf`:

- `ovh-ns1001419` remains storage-enabled and ready-enabled, preserving its
  existing Talos and Piraeus behavior.
- `ovh-ns104952` records the two verified Intel serials but keeps
  `talos_storage_enabled = false` and `piraeus_ready_enabled = false`. Its
  current XFS `UserVolumeConfig` and Talos installation behavior therefore
  remain unchanged.

The generated native `LinstorSatelliteConfiguration` proposal is
`ssd-ovh-ns104952`, pinned to both hostname `ovh-ns104952` and
`storage.allegedly.works/piraeus-ready: "true"`. The readiness label stays
absent. The current `drbd-canary` label also stays absent. The object specifies
the target's data-disk by-id path, VG/pool names, `Retain`, 4 GiB pool metadata,
and 64 KiB chunks, matching the existing pilot. It cannot match this node until
the separate readiness gate is enabled after the data disk has been verified
empty. No new cdk8s marker or eligibility registry is introduced.

## Activation checklist for a later approved maintenance

This PR prepares identities and an unmatched LSC only. It does not authorize a
node roll or any live configuration change. Before activation:

1. Refresh this inventory and verify all claims, local PVs, Pod consumers,
   CNPG roles, SeaweedFS replicas, StatefulSet availability, etcd membership,
   and both eligibility labels. Obtain an explicit per-workload handoff or
   outage decision; declared claim sizes are not a substitute for usage and
   recovery checks.
2. Review the approved maintenance plan and target-only Terraform plan. Rehearse
   Talos boot geometry on the exact serials. Keep the readiness gate false while
   the system-disk layout is introduced and the node returns healthy. Confirm
   there is no automatic reset/reinstall or raw-device initialization in the
   planned action.
3. After every consumer is evacuated and the target remains cordoned, verify
   that the exact data disk is wiped and empty before enabling pool
   initialization, regardless of the `talos_storage_enabled` setting. Add and
   review node-scoped thin-pool metrics and alert coverage, including alerts for
   absent samples, before adding `piraeus-ready=true`. Then review a separate
   target-only readiness change that allows the native LSC to initialize the
   pool while the target remains cordoned. Keep `drbd-canary` absent and confirm
   the native selector still requires the target hostname plus
   `piraeus-ready=true`.
4. After pool creation and before uncordoning the target or restoring any
   application, verify fresh metrics samples and alert-rule evaluation. Also
   verify the resulting Talos volumes, boot and etcd health, disk identities,
   PV/VG/thin-pool geometry, metadata spare, pool capacity, and retained-volume
   behavior.

Never infer that this configuration has been activated from merge, Flux
reconciliation, or the presence of the unmatched LSC object. The later
maintenance window needs its own explicit approval and live evidence.
