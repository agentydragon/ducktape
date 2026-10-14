# Workspace storage cutoff

Status: **existing Sandbox cutoff complete**, verified on 2026-10-14 at approximately 20:07 UTC.
Typed workspace template activation remains separate. This record authorizes no further deletion.

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

## Remaining activation and cleanup

1. The bounded testing matrix is complete: Pod DirectoryTree passed in
   `d8d6387e-87f1-4a3a-b73b-fae7fc62598a`, VM RawDiskImage in
   `7e47a94a-78f2-478f-af7d-14e474ce615d`, and the legacy comparison in
   `c0c5046b-52e9-4d80-8544-79a5d7eb0b30`. #10480 merged the corrected fixture. The
   [owning acceptance note](../acceptance/debug/storage_acceptance.md) records source/pins/results.
   This proves testing, not staging acceptance or existing-data migration.
2. Refresh the empty inventory and accepting controller/VM wheel pins before activating templates.
   The old templates can still create old-layout Sandboxes until reconciliation; any new consumer
   needs a separate disposition rather than silent adoption or another automatic deletion.
3. Land/reconcile the separately reviewed typed templates: Pod DirectoryTree at `/state/workspaces`
   for `runner` and `runner-ducktape`, and VM RawDiskImage/ext4 for `runner-vm`. Replace the old
   VM DataVolume workspace declaration with the Sandbox-owned declaration. Do not hide old data
   behind a new mount or copy a declaration onto an existing unclassified PVC.
4. Fresh Sandboxes get fresh UIDs and storage. Recreate finance Sandboxes only when the operator
   requests them; use new Sessions rather than claiming deleted native Sessions were revived.
   Verify reconciled declarations and mounts in both environments.
5. Retire the obsolete DataVolume-specific production path after old consumers are gone, relevant
   images are pinned, and typed-template reconciliation is verified. Keep generic KubeVirt support
   needed by other callers. Conversion and node portability remain separate future work.

Reverting activation cannot reinterpret already provisioned new workspaces: the durable layout
and identity guard must reject incompatible declarations until an explicit future conversion exists.
