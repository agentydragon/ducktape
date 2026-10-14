# Workspace storage cutoff proposal

Status: remaining cutoff is **not executed**; two separately authorized deletions are recorded below.
The operator accepted an incompatible storage
cutoff in principle. This proposal names the actual data loss and remaining execution gate before
typed workspace templates become active. It does not authorize routine Sandbox deletion.

## Observed resources

Metadata-only staging inventory refreshed on 2026-10-14 at approximately 20:02 UTC found one
remaining old-layout Sandbox, `finance-agent-2`. The earlier testing inventory found no non-fixture
Sandboxes. Testing acceptance fixtures are separate and cleaned by their exact UIDs. Both environments'
sandbox-controller is on accepting image `devel-20261014082718-1ceacb7`; staging has two Ready replicas
and testing one. Existing `runner`, staging `runner-ducktape`, and `runner-vm` templates still omit
`workspaceVolume`. They have not been activated by this proposal.

| Namespace          | Resource | Name                                     | UID                                  | Disposition if this exact cutoff is approved                 |
| ------------------ | -------- | ---------------------------------------- | ------------------------------------ | ------------------------------------------------------------ |
| agentplane-staging | Sandbox  | finance-agent-2                          | e5660f61-6466-44cd-8fc3-f38c121c2df6 | Delete through ordinary quiesce/finalizer path               |
| agentplane-staging | PVC      | finance-agent-2-state                    | 3f01e4ef-0efc-45df-8c56-85095c92ee6b | Garbage-collected with its Sandbox; all contents lost        |
| cluster-scoped     | PV       | pvc-3f01e4ef-0efc-45df-8c56-85095c92ee6b | f35cbdd9-c855-4649-83c9-73c8d2aaed03 | StorageClass local-path-ovh-hdd, 10Gi, reclaim policy Delete |

`finance-agent-2` requests `running: true`, uses `runner-ducktape`, and still has its Pod.

This Pod Sandbox has no separate workspace PVC. Its `/state/workspaces` working files, local
repositories and any uncommitted notes share its 10Gi state PVC with runner journals, native
harness persistence and bootstrap metadata. The contents were **not inspected**; this proposal
does not assume every local change was pushed. Native Sessions become impossible to resume after
the volume is removed. Quiesce flushes their final journals to the History Service; this proposal
does not drop shared PostgreSQL data or archived history, but an archive is not native harness
state or a backup of working files. Any necessary local authored work must be saved or explicitly
accepted as part of approving the concrete loss before deletion.

Its Sandbox-owned Pod, ServiceAccount, PolicyBinding, launch ConfigMap, control Service and
runner Lease follow ordinary ownership cleanup. Identity records for revocation checks are:

| Sandbox         | ServiceAccount UID                   | PolicyBinding UID                    |
| --------------- | ------------------------------------ | ------------------------------------ |
| finance-agent-2 | e41b04eb-4c1e-41e6-8a1d-7b4e681fe756 | 403aa305-4c0f-470d-a62a-8ba1853deaca |

RoleBindings and ClusterRoleBindings for the ServiceAccount are managed by
`agentplane-policy-controller`. Those native
grants lack ownerReferences and are reconciled away on PolicyBinding deletion; they are not
manually deleted by this proposal. Check the binding/subject UID annotations during revocation.
Shared Roles, PolicySets, services, ConfigMaps, Secrets and PostgreSQL PVCs are outside this list.

## Completed operator-directed deletions

The operator reported deleting a staging Sandbox; `public-coder-ducktape-1` was subsequently
absent from the refreshed inventory. Separately, the operator explicitly requested force-deletion
of `finance-agent-1` because it was wedged. Its controller was waiting for quiesce while the
Gateway returned `destination unavailable; no offline admission`.

At 20:01 UTC, deletion was requested with Sandbox UID precondition
`8faad755-2a71-4ad1-94f6-c987189b896a`, then the state-volume finalizer was removed with UID and
finalizer-list tests. This bypassed the seal; no successful final journal flush is claimed.
Ordinary garbage collection removed its Pod and PVC without force-deleting the Pod or removing
PVC protection. Follow-up reads confirmed the Sandbox, Pod, state PVC and backing PV, identity,
control Service, runner Lease, and RoleBindings/ClusterRoleBindings targeting its ServiceAccount
were absent. `finance-agent-2` was not changed. This explicit exception does not authorize bypassing
finalizers for the remaining cutoff.

## Execution and activation gates

1. The bounded testing matrix is complete: Pod DirectoryTree passed in `d8d6387e-87f1-4a3a-b73b-fae7fc62598a`,
   VM RawDiskImage in `7e47a94a-78f2-478f-af7d-14e474ce615d`, and the legacy comparison in
   `c0c5046b-52e9-4d80-8544-79a5d7eb0b30`. #10480 merged the corrected fixture. The
   [owning acceptance note](../acceptance/debug/storage_acceptance.md) records exact source/pins/results.
   Only testing is proved; no staging fixtures or existing-data migration were run.
2. Review and approve this exact Sandbox/PVC/PV loss. Refresh the inventory immediately before
   execution. Any new Sandbox, replaced UID, or changed ownership requires a revised resource
   list; a name is not a deletion precondition. This PR authorizes no additional staging deletion.
3. Arrange the short creation/send hold for this cutoff so no old-layout Sandbox is created while
   deletion and template activation are separated. It affects the named Sandbox, not shared
   service readiness. The concrete hold mechanism is part of the operator execution review.
4. Stop/delete only the approved Sandbox UID through ordinary controller behavior. Deletion may
   interrupt its in-flight work, so finish/save that work before choosing deletion. Do not remove
   a finalizer to evade an unsealable runner. Wait for the old writer, Sandbox, owned PVC/PV and
   derived identity grants to disappear before reusing the name or provisioning a new layout.
5. Land/reconcile the separately reviewed typed-template activation: Pod DirectoryTree at
   `/state/workspaces` for `runner` and `runner-ducktape`, and VM RawDiskImage/ext4 for `runner-vm`.
   The old VM DataVolume declaration is replaced by the Sandbox-owned workspace declaration.
   Do not copy a new declaration onto old data or silently hide an existing workspace mount.
6. Fresh Sandboxes get fresh UIDs and storage. Recreate the finance Sandbox through the existing
   finance preset only when the operator wants it restarted; start new Sessions, rather than
   claiming the deleted native Sessions were revived. Verify retained bindings and mounts in
   both environments, and lift the creation/send hold.
7. Retire the obsolete DataVolume-specific production path only after old consumers are gone,
   the relevant controller/guest pins are recorded, and typed-template reconciliation is verified.
   Keep generic KubeVirt support needed by other callers; no conversion or node portability work
   is added to this sequence.

Rollback before deletion is to leave current templates and storage alone. After deletion, the
native state and local files cannot be recovered by reverting manifests. Reverting activation
also cannot reinterpret already provisioned new workspaces: the durable layout/identity guard
must reject incompatible declarations until an explicit future conversion exists.
