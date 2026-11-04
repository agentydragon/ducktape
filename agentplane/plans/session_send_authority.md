# Session-addressed command authority

Status: **directory membership selected; remaining authority and fencing details need review.**
The selected public command address is a globally unique Session UUID. The native Session directory
in Dispatcher PostgreSQL is the authority for its placement. This note specifies the native send
path that can follow [SESSION_DIRECTORY_NATIVE](task_dag.md#session_directory_native--dispatcher-authority-for-environment-hosted-sessions),
and identifies decisions that still block implementation. It does not select a principal or send
policy for a central Session without an environment.

## Current boundaries

| Caller or boundary            | Current authority and behavior                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App                           | Its Thread route and access checks precede Dispatcher submission. The Go bridge uses an exact Sandbox name and UID for opening and diagnostics; command routes submit both values. The app's service token is a configured privileged Dispatcher caller, so the backend does not independently authenticate the browser user.                                                                                                                                                                                             |
| Direct CLI                    | Uses its workload token and supplies Sandbox name, UID, Session ID and command ID. Dispatcher checks a normal caller against the opening's execution ServiceAccount and the live Sandbox account.                                                                                                                                                                                                                                                                                                                         |
| Same-principal Actions        | `session_delegation` sends the retained caller's namespace, name and UID in the reviewed delegation envelope. Dispatcher checks the effective caller, not Actions' actor privilege. Action approval does not enlarge backend authority.                                                                                                                                                                                                                                                                                   |
| Orchestration Actions         | `orchestration_v1` is a documented temporary privileged service exception after an Action Decision. It verifies the named Sandbox incarnation before commands; backend grants for each operation remain separate work.                                                                                                                                                                                                                                                                                                    |
| Notification API and worker   | Subscriptions check source access, exact Sandbox ownership and live Session membership at creation. A separate `session_inbox_access.send_message` policy permits inbox append for a target Sandbox, which can lead to a service-authored notice under Notification's wake policy. It does not grant arbitrary Runtime commands or direct wake. The worker rechecks its stored destination and sends its UID with a notice command. Dispatcher authenticates Notification's service account, not the original subscriber. |
| Dispatcher                    | Queued commands and interrupts require an authorized Session opening with the exact Sandbox name and UID, including for privileged Pod-bound backends. A normal caller must match its execution principal. The queue transaction rechecks the opening binding and retains actor/effective caller attribution.                                                                                                                                                                                                             |
| Delivery, Gateway and Runtime | Dispatcher groups by Sandbox UID, rechecks live UID, lease and execution account, then calls the Gateway proxy. Gateway checks exact Sandbox UID, owner and ready Pod before forwarding and restricts Runtime Session mutation to the combined Gateway/Dispatcher ServiceAccount. Runtime journals admitted commands and rejects an unknown Session.                                                                                                                                                                      |

Source anchors: [Dispatcher API](../runner_gateway/dispatcher/api.go),
[queue transaction](../runner_gateway/dispatcher/store.go),
[delivery worker](../runner_gateway/dispatcher/delivery.go),
[Gateway proxy](../runner_gateway/gateway/proxy.go),
[Notification worker](../notification_service/worker/deliver.go),
[delegated authorization](../docs/delegated_authorization.md).

## Recommended native command contract

The external submit, model, reasoning-effort and interrupt requests name the Session UUID and a
caller-held command ID (plus the operation's arguments and an interrupt's turn ID). A caller does
not supply the Sandbox or its UID as a required public Session address. Opening still selects an
exact environment at creation; environment lifecycle and execution APIs retain their own target.
An internal delivery envelope carries the resolved namespace, name, **immutable UID** and Session
UUID. A placement generation is optional for the first native slice, whose assigned environment
cannot move. If placement can change or a retired UUID can be reused, a generation or equivalent
backend-enforced fence becomes mandatory before such operations are exposed.

At acceptance, Dispatcher authenticates the actor and derives the effective caller using the
existing direct/delegated modes. It reads the live Sandbox and execution ServiceAccount through
their existing owners; those Kubernetes reads cannot join a PostgreSQL transaction. Under the
Session lock (or equivalent serializable compare-and-swap), one Dispatcher transaction resolves
the UUID from the directory, checks the persisted placement and, for normal callers, the
execution-principal binding against the admitted caller and live-read identity, and inserts the
immutable command ID, operation/payload, exact placement UID and attribution. A concurrent directory retirement or
placement update must order before or after that transaction. A live owner change racing this
commit is caught by the persisted UID and subsequent live rechecks; no cross-store atomicity is
claimed. An opening that durably reserves the native placement can accept commands before the
owner is ready; the queue must deliver its opening first. Do not derive a new UID from a mutable
Sandbox name after acceptance. An absent, retired, or unplaced Session is not an invitation to
search History or a runner; return a stable refusal or a defined unavailable outcome. Unknown
directory or live identity state fails closed without wake. A queued receipt means only durable
Dispatcher acceptance.

**Selected for the first native cutover:** preserve the existing caller and wake rules. For a
normal direct or delegated caller, preserve the existing exact execution ServiceAccount
namespace, name and UID check against the opening and live environment. A delegated actor's
privileged allowlist membership never applies to its effective caller. Keep the existing named
App, Notification and orchestration service exceptions at their current scopes while their
caller-facing checks are separately audited; no new service or direct agent becomes privileged by
using a Session UUID. Retain actor, effective caller UID, and Action provenance on accepted work.
Resolving the directory, reading a Session or subscribing to a source is not send permission.
Dispatcher now requires a Session record for every Session-targeted command, including privileged
calls and immediate interrupts. Gateway restricts direct Runtime Session mutation to the combined
Gateway/Dispatcher workload. A Session record is distinct from a per-command receipt; this requirement
does not itself change which operations are durably queued or journaled. The public command request
still includes the Sandbox name and UID.

General destination/operation grants and revocation of previously accepted queued work remain in
`DISPATCHER_SEND_AUTHORIZATION_GENERALIZE`; retaining the existing rules does not select a new
revocation policy.

Before any wake, the delivery worker rechecks the queued row against the current directory and
live environment: Session still assigned to the **same UID**, execution account unchanged, and
the row still owned by its fenced queue worker. Keep the existing `wakeOnSend`, `resumePolicy`,
`wake_callers`, manual-stop and budget gates; a sender's ability to queue never supplies wake
authority. Check again when claiming a queued command for send. Pass the persisted UID through
the proxy, which must check the live Sandbox, owner and Pod immediately before forwarding.
Runtime admission checks the Session is actually present and journals by the same command ID.
If an environment is unavailable or suspended, leave authorized queued work queued under the
existing retry/wake policy. A missing/replaced UID or changed execution principal fails that row
without retargeting it. Do not send behind an unresolved opening. After an opening is settled,
a reachable Runtime's unknown Session is terminal for the command.

**Selected deletion-race outcomes:** once deletion closes admission, do not wake or deliver new
work to the deleted environment. Fail accepted commands proven not to have left Dispatcher; if
delivery may have reached Runtime and admission or effects cannot be established, make the outcome
observable as unknown rather than claiming failure or replaying it. Preserve any outcome established
by durable evidence, and never redirect accepted work to a replacement environment. Deletion
terminal snapshots currently reach connected `WatchDispatches` streams through transactional
PostgreSQL notifications; cleanup removes the receipts, so later reads cannot recover those
snapshots. A connected watcher behind an earlier terminal-row invalidation can also miss that
outcome when cleanup removes the row; cleanup currently snapshots only outcomes it transitions.
Broadcasting all retained receipts can overflow bounded subscriptions, so this remains a delivery
design gap. Durable post-deletion outcome visibility and retention remain open Dispatcher/History
decisions. Neither notification delivery nor Runtime admission proves that an uncertain effect failed.

There remains a narrow race between Dispatcher's directory recheck and Runtime admission. For the
first native slice, directory assignment is immutable for the lifetime of the exact environment
UID; deletion/retirement must fence new sends, wake and forwarding, and settle queued work before
the Runtime can be reused for another owner. If that invariant cannot be enforced, the receiver
must validate an authoritative placement epoch or lease at admission. A local cached generation
without such validation is not a fence. This is an implementation gate, not a reason to assume
cross-service atomicity.

An interrupt is an immediate, online control RPC with a required turn ID, not a queued input.
Apply the same UUID resolution, caller check and exact UID fence before forwarding; it must not
wake the environment. The directory may change between resolution and forwarding, so the same
deletion/placement fence applies. Keep `NO_OP` for a currently blocked target, distinguish a
runner's admission from a transport failure, and reconcile an uncertain reply by command ID and
runner journal. Cancellation and dispatch reads retain their own caller checks; they cannot be
used to discover placement without read authority.

## Recovery and decisions for review

A retry of a command ID with the same Session UUID, operation and payload returns or reconciles
the original row, including its original resolved UID. A conflicting reuse fails. Once delivery
may have reached Runtime, a lost reply or worker failure is **unknown** until durable Dispatcher,
Runtime journal or History evidence settles it; do not automatically replay an uncertain effect
or redirect it to a new owner. A same-name replacement gets a different UID and cannot adopt the
row. Preserve accepted work and the native store when handling deletion and service-store races.

The outcome and initial caller/wake rules above are selected. Remaining design and verification:

1. **Enforce the deletion fence.** Prove the old Runtime cannot admit a command after
   the selected deletion boundary, or select an authoritative receiver-checked epoch. Identify
   what enforces that boundary and where outcomes remain observable. Session movement and UUID
   reuse after deletion remain separate decisions; a local cached generation alone is not a fence.
   Ordinary managed deletion waits for the exact Runtime incarnation's final Session cutoffs,
   journal seals and History acknowledgements before removing its workload. The initial lameduck
   is earlier and still permits exact-turn interrupts. Gateway refuses Session mutation after its
   cache observes quiescing/deletion; that observation is asynchronous. Runtime component coverage
   holds an old control RPC across completed quiescence, but a bounded Gateway/Runtime/controller
   composition test remains. `deletionTimestamp` is not a cross-service linearization point, and
   manual finalizer removal or foreground deletion bypasses the managed handshake. The immediate
   interrupt's final Dispatcher recheck can race deletion and forwarding; it does not close this boundary. See [SESSION_DELETION_FENCE](task_dag.md#session_deletion_fence--prove-and-enforce-the-session-deletion-fence).
2. **Queued sender revocation.** The first cutover retains current service trust and effective-caller
   checks. Whether an accepted command is cancelled on a later user grant revocation remains a
   separate generalized-policy decision; a subscription is not a lasting send grant.
3. **Central Sessions.** A central scope with no environment has no UID or current native
   execution principal. Decide its principal, command permission and wake behavior in the central
   Session/agent-principal designs; do not invent an environment or use a privileged service
   identity as its principal. This need not block the environment-hosted slice.

Acceptance for the implementation: direct and delegated caller parity; denial for an unrelated
caller despite a known UUID; actor privilege not leaking through delegation; current App,
Notification and orchestration permissions unchanged; source-read grants unable to append, and
inbox-append grants unable to submit arbitrary commands or bypass notice wake policy; exact same-ID
and conflict behavior across restarts; denial of commands to an absent Session for privileged and
ordinary callers, including interrupts; queue/deletion and
same-name replacement races; failed directory read before wake; suspended, unavailable and
unknown Runtime outcomes; immediate interrupt denial and unknown-result reconciliation; and continued
coverage of the enforced Gateway proxy restriction. Use deterministic integration tests for the transaction and race boundary
and a bounded system test for the actual Gateway/Runtime UID fence.
