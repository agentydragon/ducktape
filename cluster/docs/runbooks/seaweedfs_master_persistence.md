# SeaweedFS master persistence

## Storage contract

Each of the three masters gets an independent 4 GiB filesystem claim on
`local-path-ovh-ssd`, mounted at `/data`. SeaweedFS uses its `-mdir`
for the Raft log, snapshots, peer configuration, current term and vote. Its
replicated state includes `MaxVolumeId` and `TopologyId`. The claim does not
hold object contents or the filer's filesystem metadata database.

For the current `seaweedfs` CR and port 9333, the operator sets `-mdir=/data`;
SeaweedFS writes `/data/m9333`. The claim template is `seaweedfs-master`, so
the claims are `seaweedfs-master-seaweedfs-master-{0,1,2}`. Each is ReadWriteOnce.

The existing local-path class binds on first consumption to an OVH SSD node.
Its reclaim policy is `Delete`: deleting a PVC deletes its backing storage.
The StatefulSet's separate Retain/Retain claim policy keeps claims on controller
deletion or scale-down; it does not protect against explicit PVC deletion.
The masters already have hard
hostname anti-affinity and tolerate the control-plane taint on these nodes.
Raft provides replication across masters; this storage must remain independent
of SeaweedFS CSI. A local PV cannot move to another node: permanent node loss
requires rebuilding that member from the surviving quorum, not deleting all
claims. Four GiB is a requested allocation; local-path does not enforce a disk
quota. Monitor the underlying node filesystem.

## Existing-cluster adoption

This is a maintenance plan, not an executed migration. Review the live operator
version and rehearse the replacement and recovery gates before scheduling it.
Enabling persistence adds an immutable StatefulSet claim template: applying the
Seaweed CR alone cannot convert the existing master StatefulSet.

**Merge gate:** rehearse and review the cutover first, and hold this CR's Flux
reconciliation before merging the persistence change. The deployed operator
(`seaweedfs-operator-0.1.42`) reconciles master replicas and Pod templates, but
does not adopt new claim templates. Its master builder hardcodes RollingUpdate
and ignores the CR's `statefulSetUpdateStrategy`. Suspending Flux does not stop
the operator. An orphan/delete followed by automatic operator recreation can
therefore roll the old Pods before a manual strategy patch reaches it.

The [master reconciler](https://github.com/seaweedfs/seaweedfs-operator/blob/seaweedfs-operator-0.1.42/internal/controller/controller_master.go)
and [StatefulSet builder](https://github.com/seaweedfs/seaweedfs-operator/blob/seaweedfs-operator-0.1.42/internal/controller/controller_master_statefulset.go)
are the version-specific contract for the procedure below. Recheck them after
an operator upgrade.

Preserve the live quorum throughout adoption. New members recover the existing
cluster's replicated state from its current leader; do not bootstrap a new
cluster, clone another member's vote/state files, or restore an unverified hot
copy of an active Raft directory.

### Preparation

1. Record the exact merged commit, operator and SeaweedFS image digests, Seaweed
   CR, master StatefulSet UID/selector/template, Pod UIDs and nodes, PVC/PV
   inventory, and controller owner references. Keep the existing Flux ownership
   and Seaweed CR identity. Do not delete the CR or change volume/filer storage.
2. Query **each master directly**, not through a load-balanced Service, at
   `/cluster/status`. Require one leader and agreement on its identity; record
   every peer and the highest `MaxVolumeId`. Record the current `TopologyId`
   from the masters' Raft recovery logs/state. Save the baseline outside
   SeaweedFS. Confirm all three masters, filers and volume servers are healthy
   and no node maintenance or other master rollout is active.
3. Establish a bounded S3 canary using a dedicated disposable object: PUT,
   read back and compare its contents, then delete only that object. Record a
   baseline read of a retained existing object as well. Credentials stay out of
   logs. A successful HTTP probe alone is not storage acceptance.
4. Verify `local-path-ovh-ssd` exists with `Delete`,
   `WaitForFirstConsumer` and the OVH/SSD topology, and that three SSD nodes
   have capacity. The class is already owned by Flux `local-path-provisioner`.

### Hold automatic replacement

Coordinate this phase around the approved PR's merge; merging is not permission
to perform maintenance. Prepare a reviewed replacement StatefulSet manifest
before starting. It must match the exact operator's persistent builder output
and the existing selector/service/member identities, except for a temporary
`OnDelete` strategy. Include the new claim template and retention policy; omit
server-assigned metadata/status and preserve the Seaweed controller owner
reference. Rehearse its creation and subsequent operator reconciliation in an
isolated cluster before using it here.

1. Suspend `ducktape-flux/seaweedfs-cluster` before the merge. Its artifact/image
   sources may continue updating; record their revisions for the later resume.
2. Temporarily stop the operator controller while changing its owned
   StatefulSet: inventory its deployment, replica count, Flux/Helm owner,
   autoscalers and other Seaweed tenants first. Arrange that the owner cannot
   immediately restore controller replicas. This affects reconciliation of
   those tenants, so the maintenance approval must include that scope. The
   storage data-plane Pods remain running. Verify no controller instance remains
   before continuing. This is not achieved by suspending the Seaweed CR's Flux
   Kustomization alone.
3. With the operator stopped, set the existing master StatefulSet itself to
   `OnDelete` and verify it. Apply the reviewed persistence fields to the same
   Seaweed CR. Do not use the ignored CR strategy field as a safety mechanism.
4. Orphan **only** `seaweedfs/seaweedfs-master` using StatefulSet deletion with
   `--cascade=orphan`. Verify all three original Pod UIDs are still running.
   Create the pre-reviewed replacement controller with `OnDelete`, new claim
   template and retained-claim policy. Check it adopts the existing Pods without
   replacing them. Do not let the operator win a create race here.
5. Restore the operator's original replica count/owner reconciliation. Verify
   it accepts the new StatefulSet without changing its Pod template or
   `OnDelete` strategy. The current reconciler leaves updateStrategy unchanged;
   verify this behavior, rather than assuming it for a different release.
   Stop if an original Pod disappears or another component changes.

### Replace one follower at a time

Determine the leader afresh before every replacement. Start with a follower;
keep the original leader and the other original follower running.

1. Delete only that follower Pod. Verify its replacement mounts its own bound
   claim at `/data`, advertises the same member address, and starts with the
   operator's persistent `-mdir` and state resumption enabled.
2. Require Raft state recovery with the **same TopologyId**, a nonempty
   persisted Raft log or valid recovered snapshot under the master's data
   directory, agreement with the live leader/peer set, and no regression in
   `MaxVolumeId`. Re-run the S3 canary and baseline read. Stop on assignment
   failures, repeated admission/election errors, identity changes or missing
   state; leave the other two original members untouched.
3. Restart that same converted follower once more. Confirm its PVC and PV UIDs
   are unchanged and logs demonstrate recovery of the existing state from disk.
   Repeat the identity, leadership and S3 checks. This checks persistence before
   risking another original member.
4. Convert the remaining follower using the same gates. Only after both
   converted followers have recovered and passed their persistent restart
   checks should the original leader be replaced. Its replacement must join
   the existing cluster after the two persistent members elect a leader.

`/cluster/status` reports leader, peers and `MaxVolumeId`, but has no committed
log index for the deployed goraft implementation. Its HTTP status is always 200. A populated peer list and Kubernetes Ready do not prove log catch-up.
The checks above combine actual Raft recovery, persistent restart, identity and
end-to-end storage behavior. If these cannot be demonstrated, stop rather than
assuming a timed delay means recovery. Never use the on-disk `conf.commitIndex`
as a current catch-up metric; it can lag live state.

### Complete and verify

1. Confirm three distinct bound claims/PVs, `Delete` reclaim policy, distinct
   node placement, persistent master commands, and unchanged cluster identity.
   Complete another bounded leader failover with S3 canary traffic and record
   the interruption duration. Check retained objects and filer/volume health.
   A persistent filesystem is not proof that the earlier six-minute election
   delay has been eliminated.
2. Restore `RollingUpdate` with partition 0 on the master StatefulSet itself;
   the CR strategy field is not effective in this operator release. Verify all
   Pods already match the persistent Pod template before allowing rolling
   updates. Resume
   `ducktape-flux/seaweedfs-cluster` at the reviewed revision. Check the rendered
   persistence configuration remains applied and there is no unexpected rollout.
3. Keep PVs, claims and diagnostic evidence. The StatefulSet claim retention
   policy is not a backup or protection against PVC deletion. Record permanent-node-replacement and
   disaster-recovery procedures separately before relying on unattended recovery.

## Stop and recovery

On a failed gate, stop replacing members and keep Flux suspended for this CR.
Preserve every surviving Pod and persistent claim. Diagnose from the remaining
quorum; do not reset Raft logs, use forced bootstrap, scale to zero, or cascade
delete the StatefulSet. Reverting the Git fields cannot undo the StatefulSet
conversion or recover destroyed ephemeral state. Recovery uses the surviving
cluster and the same retained member claims, with an incident-specific plan.
