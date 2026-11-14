# EnvironmentService in the Sandbox Controller

Status: proposed implementation contract for review. The direction to put EnvironmentService in
the Sandbox Controller is agreed; no traffic or ownership has moved yet. This plan describes the
prerequisites and rollout, not current behavior.

## Current coupling and intended boundary

The Runner Gateway serves `EnvironmentService` and its Dispatcher calls the same process's
lifecycle manager to wake a Sandbox for queued work. The lifecycle manager calls back into the
Dispatcher's PostgreSQL store to reserve a shared wake budget. EnvironmentService's identity change
runs a Kubernetes patch inside the Dispatcher's per-Sandbox admission lock. These are behavioral
boundaries, not just imports: preserve them across the process split.

The Sandbox Controller will serve the existing EnvironmentService API and own its inventory,
management and lifecycle decisions. Dispatcher retains Session admission and its durable wake
budget. The Gateway remains available for Dispatcher and Runtime proxying. The EnvironmentService
protobuf service and existing client method names do not change as a consequence of moving the Go
packages. Identity, wake-budget and retained Session data are preserved.

## Calls and authority

**Automatic wake.** Dispatcher calls a narrowly scoped internal wake RPC on the controller for a
specific Sandbox namespace, name and UID, carrying the admitted command's effective caller and
Session provenance. The controller authenticates Dispatcher as the actor, verifies that actor is
allowed to invoke this method and to delegate the particular caller, revalidates the live caller
incarnation and the Sandbox binding, then applies the same automatic-wake policy as today. It must
not treat the Dispatcher's own privileged account as the effective caller. Explicit
`ResumeEnvironment` keeps its existing separate authorization and is not widened to Identity
callers by this internal method. Denied, not-needed, rate-limited, requested and unknown outcomes
remain distinguishable to the delivery worker.

The existing shared delegation envelope can represent an Identity with Session provenance; it cannot
currently represent an effective ServiceAccount. Automatic wake supports both. The implementation
must first specify and review how the existing delegated authorization contract represents the
retained ServiceAccount name, namespace and UID for this method, with a trusted actor and Session
provenance. It must use shared verification, reject missing/replaced UIDs and keep actor and caller
separate. Do not silently substitute the Dispatcher account, forward an expired workload token, or
invent an unchecked identity header. Until that extension is reviewed and tested, the automatic
wake cutover is blocked.

**Wake budget.** Dispatcher remains the durable budget owner. The controller calls a private,
authenticated, controller-only reservation RPC with the caller-incarnation key, Sandbox UID and
next wake generation after policy approval and before the optimistic Sandbox patch. Its result
retains the existing wait duration and failure behavior. Old and new lifecycle instances use the
same PostgreSQL reservation path, so concurrent explicit and automatic wakes and mixed-version
replicas share a budget. A reservation keyed by the same caller, Sandbox and generation must keep
its current duplicate behavior. Backend unavailability fails closed; a caller cannot mint budget
reservations through a public EnvironmentService method.

**Identity change.** A check followed by an unlocked controller patch is insufficient: a Session can
be admitted between them. The controller authenticates and authorizes the original
`SetEnvironmentIdentity` caller, resolves the requested Identity to its exact UID, then calls a
private Dispatcher identity-change operation. Dispatcher takes the same exclusive Sandbox UID
advisory lock used today, checks retained Sessions under that lock, and performs the guarded
Kubernetes patch before releasing it. The operation accepts the exact Sandbox UID and expected
resource version plus the desired Identity name and UID; it re-reads and verifies the target and
Identity as necessary before patching. `CreateSession` continues to take the shared lock and recheck
the live binding. A same-name replacement, a conflict, failed Session query or unavailable Dispatcher
must not turn into a successful unguarded mutation. The private operation admits only the controller
workload; it is not a new user-facing identity-change API. Preserve the existing refusal and conflict
semantics and limits on listed Session IDs.

These internal calls must be bounded and cancellation-aware. The wake call cannot hold a Dispatcher
transaction or its per-Sandbox admission lock across the outbound call: the controller may call back
to Dispatcher for a budget reservation. A lost wake or identity-patch response is an unknown outcome;
retain the current observation/reconciliation rules rather than replaying a mutation merely because
an RPC failed. Reservation retry is allowed only where its existing generation key proves the same
reservation. Test each failure point explicitly.

## Serving and rollout

The controller is a leader-elected controller-runtime manager, but EnvironmentService serves on
every ready replica. Give the gRPC server its own explicit non-leader-elected lifecycle, readiness,
TLS identity, destination audience, TokenReview/SVID and delegation configuration, Service and
projected credentials. Its watches and live reads must preserve existing freshness and stream
revalidation. Restrict the new internal RPCs by authenticated workload and method. Grant only the
Kubernetes operations each service needs, including an uncached read/watch where the present API
requires it. Readiness means the listener and its required dependencies are usable; a follower must
not wait for controller leadership before serving. Shutdown stops accepting requests, drains streams
and joins serving work without blocking reconciliation handoff indefinitely.

Deliver in independently reviewable pieces:

1. Land and test the internal budget and identity-fence operations and the delegated-caller contract;
   deploy receivers before senders. Both old and new code must use the same underlying locks and
   budget records throughout the overlap.
2. Deploy a controller image capable of serving the complete API and internal wake method behind an
   initially unused Service. Add settings in code before manifests set them. Exercise it with the
   app unavailable and verify auth, readiness and multi-replica behavior before routing clients.
3. Retarget the Dispatcher wake path and all EnvironmentService clients, with destination audiences,
   credentials, configuration, cdk8s, RBAC and system diagrams updated. Verify staging traffic,
   wake and identity fences before retiring old serving. Preserve existing Sessions, Sandboxes,
   identities and database data; follow [staging data preservation](../docs/staging_data_preservation.md).
4. Once traffic has left and rollback conditions are met, remove the old Gateway service wiring and
   obsolete access/configuration, and place the remaining Gateway command once. Remove settings from
   manifests before removing their code. Do not combine an RBAC removal with a deployment that still
   needs the grant.

Rollback restores clients to the old endpoint while its compatible serving path and budget/lock
receiver remain available. Before each cleanup, record the last safe rollback image/config pair and
verify Flux rollout and endpoint readiness. After cleanup, rollback requires restoring the compatible
receivers and configuration first; it must not discard state.

## Acceptance before cutover

- Race a Session admission against an Identity change in both orders, including replacement UIDs,
  stale resource versions, Dispatcher loss and lost responses; no different-Identity Session can be
  admitted in the check/patch gap.
- Exercise explicit and automatic wake together from different replicas and through both old and new
  serving paths; prove shared limits, generation deduplication, caller revocation, target replacement,
  operator-requested stop and outcome-unknown behavior. Cover both Identity and ServiceAccount
  effective callers.
- Verify the controller serves from non-leaders, stream authorization is rechecked, dependencies fail
  closed, and shutdown and leader transitions preserve serving and reconciliation lifetimes.
- Run the existing EnvironmentService, lifecycle, Dispatcher storage and Sandbox Controller tests and
  system scenarios for bootstrap, wake, suspension, quiescence and retained Sessions. Include a
  mixed-version rollout and rollback with the app unavailable. Update the system overview in the PR
  that changes traffic.
