# Session-addressed command authority

Status: **first native cutover implemented; outcome visibility, revocation, central Sessions and
post-deletion inbox sends remain open.** Commands address a globally unique Session UUID, and the
Dispatcher PostgreSQL Session directory is the placement authority. The implemented caller,
binding, deletion-fence and possible-forwarding rules are the
[Dispatcher SPEC](../runner_gateway/dispatcher/SPEC.md); delegated callers follow the
[delegated authorization contract](../docs/delegated_authorization.md).
[`HARNESS_IDENTITY_DESIGN`](task_dag.md#harness_identity_design--decide-private-session-to-store-and-native-mapping)
tracks the private harness-store and native-ID mapping. Nothing here selects a principal or send
policy for a central Session without an environment.

The native rules describe the first slice, not the final lifetime or authority of an
Environment-independent Session. Coordinate Environment deletion with
[`SESSION_AND_INBOX_LIFETIME`](task_dag.md#session_and_inbox_lifetime--decide-session-and-inbox-lifetimes)
and cross-principal Session targets with
[`SESSION_ACCESS_GROUPING_DESIGN`](task_dag.md#session_access_grouping_design--access-groups-across-independent-sessions-and-environments).

## Gates before placement can move

The first slice relies on placement being immutable for the lifetime of the exact environment UID,
with deletion fencing new sends, wake and forwarding before the Runtime can serve another owner. A
narrow race remains between Dispatcher's directory recheck and Runtime admission. If placement can
change or a retired UUID can be reused, a placement generation or equivalent backend-enforced fence
validated by the receiver at admission becomes mandatory before such operations are exposed. A local
cached generation without that validation is not a fence; do not assume cross-service atomicity.

## Recovery and decisions for review

1. **Complete bounded outcome visibility.** Deletion terminal snapshots reach only connected
   `WatchDispatches` streams, and a watcher behind an earlier terminal-row invalidation can miss
   its outcome when cleanup removes the row ([SPEC](../runner_gateway/dispatcher/SPEC.md#possible-command-forwarding-and-deletion-outcomes)).
   Broadcasting all retained receipts can overflow bounded subscriptions. Durable post-deletion
   outcome visibility and retention remain open Dispatcher/History decisions. Neither notification
   delivery nor Runtime admission proves that an uncertain effect failed. Explicit Session deletion
   is not implemented; callers must not deliberately reuse its UUID if added later.
   `deletionTimestamp` is not a cross-service linearization point, and manual finalizer removal or
   foreground deletion bypasses the managed handshake. See
   [SESSION_DELETION_FENCE](task_dag.md#session_deletion_fence--prove-and-enforce-the-session-deletion-fence).
2. **Queued sender revocation.** The first cutover retains current service trust and effective-caller
   checks. Whether an accepted command is cancelled on a later user grant revocation remains a
   separate generalized-policy decision in
   [`DISPATCHER_SEND_AUTHORIZATION_GENERALIZE`](task_dag.md#dispatcher_send_authorization_generalize--replace-v0-service-trust-with-scoped-sender-grants);
   a subscription is not a lasting send grant.
3. **Central Sessions.** A central scope with no environment has no UID or current native
   execution principal. Decide its principal, command permission and wake behavior in the central
   Session/agent-principal designs; do not invent an environment or use a privileged service
   identity as its principal. This need not block the environment-hosted slice.

Acceptance coverage to confirm for the implemented slice: direct and delegated caller parity;
denial for an unrelated caller despite a known UUID; actor privilege not leaking through delegation;
current App, Notification and orchestration permissions unchanged; source-read grants unable to
append, and inbox-append grants unable to submit arbitrary commands or bypass notice wake policy;
same-ID and conflict behavior across restarts; denial of commands, including interrupts, to an
absent Session for privileged and ordinary callers; queue/deletion and same-name replacement races;
failed directory read before wake; suspended, unavailable and unknown Runtime outcomes; and
unknown-result reconciliation. Use deterministic integration tests for the transaction and race
boundary; the deployed [Session deletion suite](../system_tests/suites/test_session_deletion.py)
covers the Gateway/Runtime UID fence.

## Inbox sends after the required Environment is gone

**Decision for review; no permission change is selected here.** The existing
`session_inbox_access` rules target a live Sandbox by name/UID, inbox tag or all Sandboxes.
Notification resolves the target Session's exact placement and reads that Sandbox before applying
those rules. When it is deleted, there is no Sandbox object or current tag set to evaluate. An
existing subscription authorizes reading its source, not sending messages. The selected Session and
inbox lifetime does not itself grant a sender access, and an unknown or unavailable policy view must
still fail closed.

Compare these choices:

1. **Session-scoped grants and grouping (recommended):** define independently managed Session
   targets or groups, and evaluate current PolicyBindings for each new send against the retained
   Session and its exact owner. Manage membership independently of any Environment, define its
   authority and revocation, and retain accepted message attribution and idempotency. A grant to send
   grants neither inbox read/acknowledgement nor arbitrary Session commands or wake.
2. **Exact owner only:** allow sends from the Session's exact execution ServiceAccount, and continue
   refusing cross-account sends until Session-scoped grants are designed. This is a narrower first
   slice, but same-account membership alone must be an explicitly approved grant, not inferred from
   knowing the Session ID.
3. **Retained Sandbox targets/tags:** preserve historical Sandbox membership and apply current sender
   bindings against it. This needs an owner for edits and revocation after deletion, and its semantics
   can outlive or broaden the original Sandbox grant; do not adopt a frozen tag snapshot as current
   authority by default.

For any choice, evaluate authorization at message acceptance, keep the sender's authenticated
identity, retain the accepted message independently of later revocation, and preserve retries under
its original idempotency key. Do not create a new inbox for an unproven or ownerless Session, infer
ownership from a currently existing same-name ServiceAccount, wake a nonexistent Environment,
retarget accepted notices to a replacement UID, or replay a possibly delivered notice. Decide how
Session deletion affects future sends and pending notices separately from payload retention.

Before implementation, review direct and delegated sender identity, same-account and cross-account
examples, group membership edits and revocation, policy-watch failure, ServiceAccount replacement,
concurrent deletion and send, accepted retries after revocation, unavailable placement and unknown
notice outcomes. The operator-facing policy target and authority belong to
[`SESSION_ACCESS_GROUPING_DESIGN`](task_dag.md#session_access_grouping_design--access-groups-across-independent-sessions-and-environments),
not to a Notification-only interpretation of the old Sandbox rule.
