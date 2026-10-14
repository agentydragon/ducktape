# Workspace storage cutoff

Status: **existing Sandbox cutoff complete**, verified on 2026-10-14 at approximately 20:07 UTC.
Typed workspace templates from #10507 subsequently reconciled in both environments; see
[rollout evidence](../acceptance/debug/storage_acceptance.md#typed-template-rollout).
This record authorizes no further deletion.

## Completed operator-directed deletions

The operator reported deleting a staging Sandbox; `public-coder-ducktape-1` was subsequently
absent from the refreshed inventory. The operator then explicitly requested force-deletion of
`finance-agent-1`, whose controller was waiting for quiesce while the Gateway returned
`destination unavailable; no offline admission`.

At 20:01 UTC, deletion was requested with Sandbox UID precondition
`8faad755-2a71-4ad1-94f6-c987189b896a`, then the state-volume finalizer was removed with UID and
finalizer-list tests. This bypassed the seal; no successful final journal flush is claimed.
Ordinary garbage collection removed its Pod and PVC without force-deleting the Pod or removing
PVC protection. Follow-up reads confirmed the Sandbox, Pod, state PVC and backing PV, identity,
control Service, runner Lease, and RoleBindings/ClusterRoleBindings targeting its ServiceAccount
were absent. This explicit exception does not authorize routine finalizer bypasses.

The operator subsequently reported deleting the last Sandbox, `finance-agent-2`. Follow-up reads
confirmed both `agentplane-staging` and `agentplane-testing` contain no Sandboxes. The second
finance Sandbox's Pod, PVC, backing PV, ServiceAccount, PolicyBinding, control Service, runner
Lease and ServiceAccount-subject RoleBindings/ClusterRoleBindings are absent. Its recorded
resource identities were:

| Resource | Name                                     | UID                                  |
| -------- | ---------------------------------------- | ------------------------------------ |
| Sandbox  | finance-agent-2                          | e5660f61-6466-44cd-8fc3-f38c121c2df6 |
| PVC      | finance-agent-2-state                    | 3f01e4ef-0efc-45df-8c56-85095c92ee6b |
| PV       | pvc-3f01e4ef-0efc-45df-8c56-85095c92ee6b | f35cbdd9-c855-4649-83c9-73c8d2aaed03 |

Staging now has only the shared `postgres-1` and `postgres-2` PVCs. Shared PostgreSQL and archived
history were not deleted. The removed state PVCs also held workspace files and native harness
persistence; reverting manifests cannot restore them. No content backup or successful seal for
the operator's deletions is claimed.

## Completed activation and remaining cleanup

The bounded testing matrix passed for Pod DirectoryTree, VM RawDiskImage, and the legacy
DataVolume comparison; exact invocations and limitations are in the
[owning acceptance note](../acceptance/debug/storage_acceptance.md). #10507 activated Pod
DirectoryTree at `/state/workspaces` and VM RawDiskImage/ext4. Read-only verification on
2026-10-14 found those declarations and attachments reconciled in both environments, with no
Sandboxes, VMs, VMIs or DataVolumes remaining. This proves template reconciliation, not staging
workload acceptance or existing-data migration.

Retire the obsolete DataVolume-specific production path after checking all remaining code callers.
Keep generic KubeVirt support needed elsewhere. Conversion and node portability remain separate
future work. Fresh Sandboxes get fresh UIDs and storage; recreate finance Sandboxes only when the
operator requests them, using new Sessions rather than claiming deleted native Sessions revived.

Reverting activation cannot reinterpret already provisioned new workspaces: the durable layout
and identity guard must reject incompatible declarations until an explicit future conversion exists.
