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

## 2026-10-19 execution record

The migration completed at 08:20 UTC on Kubernetes 1.35.1, with SeaweedFS 4.46
and operator 1.0.39 (chart 0.1.42). Configuration came from PR #11074, commit
`c532a68f42cf9599c7fb18961e8e096c5eaf2baa`.

| Master | Node            | Claim                                 |
| ------ | --------------- | ------------------------------------- |
| 0      | `ovh-ns104963`  | `seaweedfs-master-seaweedfs-master-0` |
| 1      | `ovh-ns104952`  | `seaweedfs-master-seaweedfs-master-1` |
| 2      | `ovh-ns1001419` | `seaweedfs-master-seaweedfs-master-2` |

All three claims were Bound, independent 4 GiB `local-path-ovh-ssd` volumes.
The original TopologyId `44018856-e2c1-48e4-bbda-b26b90bd801e` and MaxVolumeId
1116 were preserved. Masters 1 and 2 passed same-PVC/PV restart checks, including
recovery logs and preservation of their existing Raft WAL prefixes. Original
leader 0 was converted last. With it absent, persistent master 1 became leader
and baseline S3 reads plus fresh 256 KiB PUT/GET checks passed before recreation.
After all masters were persistent, current leader 1 was deleted again; master 0
became leader and the same S3 checks passed before master 1 was recreated on its
unchanged PVC/PV, preserving its WAL prefix.

The final StatefulSet UID was `6f6920bd-cd3e-4e5c-b0f7-8847740d2b0c`, with
revision `seaweedfs-master-84b8d5ccf9`. Adoption, operator reconciliation and
restoration of RollingUpdate preserved the three converted Pod UIDs. Final
checks found three Ready and updated replicas with zero container restarts.
The original Helm/Flux suspension fields and operator replica count were
restored, with reconciled generations and Ready conditions verified. Final S3
checks passed, the retained baseline object's hash was unchanged, and only the
migration's disposable canary was deleted, with HEAD returning 404.

Checks ran at maintenance checkpoints, not continuously; the actual request
interruption duration was not measured. The frozen-process limitation described
below remains unresolved. Temporary migration/rehearsal code is retained in the
history of PRs #11086 and #11088 rather than installed as permanent tooling.

### Preparation incident

At 07:23 and 07:24 UTC, raw DELETE requests intended as dry-runs actually deleted
the original StatefulSet and master 1. The requests put `dryRun=All` in the query
but supplied nonempty DeleteOptions bodies without `dryRun`. The
[Kubernetes 1.35.1 handler](https://github.com/kubernetes/kubernetes/blob/v1.35.1/staging/src/k8s.io/apiserver/pkg/endpoints/handlers/delete.go)
decodes the body instead of the query when a body exists. A raw DELETE dry-run
must include `"dryRun": ["All"]` in its DeleteOptions body; a query parameter
alone is insufficient in this case.

The original masters 0 and 2 survived and retained quorum. The operator recreated
the persistent StatefulSet, whose immutable-volume reconciliation prevented
recreation of master 1. The controller was changed to OnDelete, master 1 was
restored with its persistent claim, and original identity and S3 checks passed.
The controlled migration then paused the owners/operator, orphaned that new
controller and followed the rehearsed sequence below. This incident is separate
from the successful disposable rehearsal; no continuous availability measurement
establishes the absence of user-visible errors during it.

## Existing-cluster adoption procedure

This records the version-specific procedure used for the conversion. It is not
a procedure to rerun on the already-persistent cluster. Review the live operator
version and rehearse replacement and recovery before another conversion.
Enabling persistence adds an immutable StatefulSet claim template: applying the
Seaweed CR alone cannot convert the existing master StatefulSet.

The persistence configuration has already merged in PR #11074. At the initial
inspection the operator tried to add the mount without its claim template, so
Kubernetes rejected the update with `volumeMounts[0].name: Not found`. The three
original ephemeral master Pods were still running. Check current live state
before following this procedure. The deployed operator
(`seaweedfs-operator-0.1.42`) reconciles master replicas and Pod templates, but
does not adopt new claim templates. Its master builder hardcodes RollingUpdate
and ignores the CR's `statefulSetUpdateStrategy`. Suspending Flux does not stop
the operator. An orphan/delete followed by automatic operator recreation can
therefore roll the old Pods before a manual strategy patch reaches it.

Do not immediately adopt the old ephemeral Pods into a controller with claim
templates, even with `OnDelete`. The StatefulSet controller tries to reconcile
their volumes, receives an immutable-Pod-spec error, and stops before recreating
a deleted later ordinal. This was reproduced in the real-Kubernetes rehearsal
[2857285e](https://app.buildbuddy.io/invocation/2857285e-3c0d-4b60-b18a-ffffb46a6512),
including with Parallel pod management. Keep the controller absent until all
three Pods have compatible persistent volumes.

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
3. Establish a bounded S3 canary using a dedicated disposable object. Save a
   random 256 KiB payload, its unique object key and a retained baseline object's
   hash outside SeaweedFS. At each checkpoint, read the canary before rewriting,
   compare its contents, perform a fresh PUT/GET comparison and verify the
   baseline hash. At completion, check again, delete only the owned canary and
   require HEAD to return 404. Preserve the state and payload until cleanup;
   keep credentials out of logs. A successful HTTP probe alone is not storage
   acceptance.
4. Verify `local-path-ovh-ssd` exists with `Delete`,
   `WaitForFirstConsumer` and the OVH/SSD topology, and that three SSD nodes
   have capacity. The class is already owned by Flux `local-path-provisioner`.

### Hold automatic replacement

Obtain maintenance authorization independently of merging. Prepare a reviewed
replacement StatefulSet manifest before starting. It must match the exact
operator's persistent builder output
and the existing selector/service/member identities, except for a temporary
`OnDelete` strategy. Include the new claim template and retention policy; omit
server-assigned metadata/status and preserve the Seaweed controller owner
reference. Rehearse its creation and subsequent operator reconciliation in an
isolated cluster before using it here.

Derive each member's PVC/Pod pair from the reviewed final controller's claim and
Pod templates. Preserve selector, member hostname, peer subdomain, image, flags
and placement constraints. Give each member its own claim and omit controller
ownership and revision labels until final adoption. Reject mismatched Seaweed
ownership, unexpected existing claims, unsupported persistence fields and
overlapping mounts. Compare the proposed manifests to fresh live snapshots;
do not copy another member's Raft files.

The rehearsal runs three master members on one Kubernetes node with independent
local-path claims. The remote executor lacks kube-proxy's kernel support, so the
fixture uses node-IP API/DNS access, a headless filer Service and an S3 Pod
port-forward. It exercises the real operator, Raft recovery and S3 data path;
it does not validate ClusterIP routing, production SSD placement or cross-node
failure isolation. Check those production properties separately.

The [2026-10-19 rehearsal](https://app.buildbuddy.io/invocation/51c19584-9230-41ec-9a8b-d184bc7b9df3)
passed against SeaweedFS 4.46, operator 1.0.39 (chart 0.1.42), and Kubernetes
1.34.1. It verified both followers' cold conversion and same-PVC/PV restart,
retention of the existing Raft WAL prefix and TopologyId, S3 baseline reads and
fresh writes while the original leader was absent, and another current-leader
deletion after all members were persistent. Final controller adoption, operator
reconciliation and restoration of RollingUpdate preserved all three converted
Pod UIDs. Production runs Kubernetes 1.35.1; this is rehearsal evidence, not live
migration acceptance.

A separate [SIGSTOP experiment](https://app.buildbuddy.io/invocation/d36367cb-aa14-48fe-ac81-a0b5fafc5d3f)
did **not** pass S3 write acceptance: the surviving masters elected a leader,
but clients retained TCP connections to the frozen process and a write timed
out. Pod termination closes those connections; the passing deletion rehearsal
does not establish availability during a network blackhole or frozen process.
Do not treat persistence as a fix for that failure mode.

1. Save the exact original suspension fields, including whether a field is
   absent. The current ownership chain starts at the self-managed
   `flux-system/flux-system` Kustomization, which declares both child
   Kustomizations with `suspend: false`. Suspend that root first and wait for
   any in-flight reconciliation to finish; suspending only a child can be
   reverted by its parent. Suspend `ducktape-flux/seaweedfs-cluster` and
   `ducktape-flux/seaweedfs-operator`, then the
   `seaweedfs/seaweedfs-operator` HelmRelease. Verify the holds persist. Sources
   may keep updating; record their revisions for the later resume. Independently
   reconciling application Kustomizations continue running.
2. Record the operator Deployment UID, replicas, image, autoscalers and other
   Seaweed tenants. Scale only `seaweedfs/seaweedfs-operator` to zero and verify
   no controller instance remains. This pauses reconciliation for all Seaweed
   tenants; their data-plane Pods continue running. Suspending Flux alone does
   not stop the operator. Keep the Flux/Helm holds until migration completes;
   restart the operator only after all three persistent Pods have been adopted.
3. With the operator stopped, set the existing master StatefulSet itself to
   `OnDelete` and verify it. Apply the reviewed persistence fields to the same
   Seaweed CR. Do not use the ignored CR strategy field as a safety mechanism.
4. Orphan **only** `seaweedfs/seaweedfs-master` using StatefulSet deletion with
   `propagationPolicy: Orphan` and a `DeleteOptions.preconditions.uid` matching
   the recorded controller. Verify all three original Pod UIDs are still running.
   Keep the StatefulSet absent during member conversion. The prepared standalone
   Pod manifests have the same names, hostnames, peer subdomain and persistent
   template as the final controller, with independent claim names. They have no
   controller owner or fabricated revision label.
5. Verify the operator remains stopped and the StatefulSet absent before every
   member replacement. This longer operator hold delays reconciliation of all
   its tenants, while their existing data-plane workloads continue running.

### Replace one follower at a time

Determine the leader afresh before every replacement. Start with a follower;
keep the original leader and the other original follower running.

1. Create only that follower's reviewed PVC. Check its current Pod UID and
   use that UID as the deletion precondition. Delete only that Pod, then create
   its reviewed standalone Pod, saving the Pod
   creation API response outside
   SeaweedFS. A new claim may remain Pending until the Pod is scheduled because
   this class binds on first consumption. Verify the Pod mounts its own bound
   claim at `/data`, advertises the same member address, and starts with the
   operator's persistent `-mdir` and state resumption enabled. The controller is
   intentionally absent; do not wait for it to create a replacement.
2. Require Raft state recovery with the **same TopologyId**, a nonempty
   persisted Raft log or valid recovered snapshot under the master's data
   directory, agreement with the live leader/peer set, and no regression in
   `MaxVolumeId`. Re-run the S3 canary and baseline read. Stop on assignment
   failures, repeated admission/election errors, identity changes or missing
   state; leave the other two original members untouched.
3. Restart that same converted follower once more by deleting only its Pod and
   creating the same reviewed standalone Pod. Reuse its existing claim; do not
   delete or recreate it. Save the latest creation response. Confirm its PVC and
   PV UIDs
   are unchanged and logs demonstrate recovery of the existing state from disk.
   Preserve the local Raft log before and after the restart and verify the
   original TopologyId remains in both. Goraft WAL replay logs this through
   `TopologyId applied on follower` or `TopologyId generated and applied on leader`;
   `Recovered TopologyId` messages describe snapshot recovery and need not appear
   when no snapshot exists. The `/dir/status` endpoint proxies to the leader, so
   querying it on a follower does not prove that follower recovered local state.
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
   Verify the leader replacement elected a persistent member and passed S3
   checks. Check retained objects and filer/volume health.
   Once all three members are persistent, complete a bounded current-leader
   failover with S3 traffic and record its outcome and interruption duration.
   A persistent filesystem is not proof that the earlier six-minute election
   delay has been eliminated.
2. Create the reviewed final StatefulSet with `OnDelete`. Verify it adopts all
   three persistent Pod UIDs without replacing them. Restore the operator's
   original replica count while keeping owner holds. Verify it reconciles
   successfully without changing the Pod template or `OnDelete` strategy.
3. Compare each Pod's spec to its saved creation response, allowing only the
   scheduler's assigned `nodeName`, and verify it was created from the reviewed
   persistent template. Only these verified new Pods may receive the final
   controller's `status.updateRevision` as their `controller-revision-hash`
   label. Standalone creation did not have a controller revision to assign;
   never relabel an old or mismatched Pod to conceal a pending update. Require
   three updated replicas and unchanged Pod UIDs.
4. Restore `RollingUpdate` with partition 0 on the master StatefulSet itself;
   the CR strategy field is not effective in this operator release. Verify all
   Pods already match the persistent Pod template before allowing rolling
   updates. Restore the HelmRelease's original suspension field, then both child
   Kustomizations, then the root Kustomization last. Restore absent fields as
   absent rather than blindly setting them to false. Check the reviewed source
   revision, rendered persistence configuration, operator replica count and
   absence of an unexpected rollout.
5. Finish the S3 canary to verify and delete only its own disposable object.
   Keep PVs, claims and diagnostic evidence. The StatefulSet claim retention
   policy is not a backup or protection against PVC deletion. Record permanent-node-replacement and
   disaster-recovery procedures separately before relying on unattended recovery.

## Stop and recovery

On a failed gate, stop replacing members and keep the owner holds. While any
orphaned Pod still lacks the new storage, keep the operator stopped and the
StatefulSet absent; automatic recreation would reintroduce the incompatible
adoption. Report this reconciliation hold explicitly during incident handoff.
Preserve every surviving Pod and persistent claim. Diagnose from the remaining
quorum; do not reset Raft logs, use forced bootstrap, scale the masters to zero,
or cascade
delete the StatefulSet. Reverting the Git fields cannot undo the StatefulSet
conversion or recover destroyed ephemeral state. Recovery uses the surviving
cluster and the same retained member claims, with an incident-specific plan.
