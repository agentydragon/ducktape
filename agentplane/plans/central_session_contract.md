# Central Session contract: creation, attachments, lifetime

Three operator decisions drafted together, each with its alternatives, one recommendation and the
question for review. They build on what is settled: a Session's principal is one Agentplane
`Identity` named at `CreateSession`
([`SESSION_IDENTITY_CONTRACT`](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission));
same-Identity messaging and the wake it causes are implied and a cross-Identity send needs
`session_inbox_access`; lifecycle control needs an explicit `environment` rule
([`ENVIRONMENT_RULE_TYPE_DESIGN`](task_dag.md#environment_rule_type_design--explicit-environment-lifecycle-rules));
the central sampler acts for a Session's Identity as a delegating actor and never holds its key
([`CENTRAL_SESSION_ACTING_DESIGN`](task_dag.md#central_session_acting_design--a-central-session-acting-as-its-identity));
Dispatcher PostgreSQL and queue-backed admission are mandatory; Kubernetes stays authoritative for
policy objects; acting as an Identity is not ownership of any environment. Implemented baselines are
the [Dispatcher SPEC](../runner_gateway/dispatcher/SPEC.md), the
[sampler SPEC](../central_sampler/SPEC.md) and the
[Notification README](../notification_service/README.md); the wider model is
[Sessions & environments](session_environment_model.md).

The running example is the daily CI agent ([priorities](task_dag.md#stakeholder-priorities-2026-10-30-pdt)):
Identity `ci-daily` in the policy namespace, Session `S` created by the operator from the app, and a
build Environment `ci-build` (Sandbox UID `e1`) bound to `ci-daily`. Each step names the implemented
operation, or the addition and the backend that owns it. Fresh per-Session Identities are
[`SESSION_IDENTITY_PROVISIONING_DESIGN`](task_dag.md#session_identity_provisioning_design--session-scoped-identities),
out of scope here.

## 1. `CENTRAL_SESSION_CREATION_CONTRACT`: opening with zero Environments

### Alternatives

- **A. A new placement variant with nothing environmental in the spec.** `SessionPlacement.kind`
  gains `central_sampler {}`; `central_sampler_v1` is retired in the same cutover and each existing
  V1 executor becomes that Session's first attachment (section 2). The Session names its Identity in
  `CreateSessionRequest`.
- **B. Make `central_sampler_v1.executor_environment` optional.** The smallest proto diff, but a
  flag-plus-optional shape: every reader branches on "executor present", V1's Sandbox-derived
  identity and wake semantics stay entangled with the field, and a later attachment duplicates it.
- **C. Keep V1 beside a zero-Environment placement.** Two central placements with different
  identity sources for as long as setup ownership is undecided; every consumer handles both.

**Recommendation: A.** The spec stays immutable and placement-only; everything that can change
lives in attachments.

### Contract

```text
CreateSessionRequest {
  session_id: UUID                     // caller-chosen, canonical lowercase (implemented)
  spec: SessionSpec {
    placement: central_sampler {}      // addition: proto/session.proto
    instructions, model, reasoning_effort
    cwd: must be empty                 // INVALID_ARGUMENT otherwise
  }
  identity: "ci-daily"                 // addition: Dispatcher; a name in the Gateway's policy_namespace
  setup_script: must be absent         // INVALID_ARGUMENT otherwise
}
```

- **Who may name which Identity.** A privileged caller (the app on the operator's behalf, the
  system-test driver) may name any existing Identity. An Identity caller, direct or through a
  delegating actor such as `orchestration_v1`, needs the `environment` rule's `create_session`
  operation with target `boundIdentity: ci-daily`, read as "create a Session running as that
  Identity"; the Session names that Identity and no other, and zero Environments is the degenerate
  case of "on an environment bound to it". Nobody selects a privileged Identity by naming it.
  Dispatcher resolves the name through its live Identity reader and records name and UID; an absent
  Identity is `FAILED_PRECONDITION` before any row is written. Additions: Dispatcher's
  `CreateSession` admission (today an Identity caller is always `PERMISSION_DENIED`) and
  `orchestration_v1`'s create input in the Action Service gain the Identity and the rule check.
- **Immutable request and retry identity.** The idempotency comparison is the implemented one
  (Session ID, deterministic spec bytes) plus the Identity name. An exact retry returns the stored
  Session with the Identity UID recorded at creation. A retry after a same-name Identity replacement
  still finds it; its work then fails closed `execution_identity_changed` (section 3).
- **Dispatcher logical admission.** The commit creates the Session with no environment: no
  executor read, no inventory lookup, no wake assessment, `required_environment` unset.
  Materialization starts `WAITING` and the owner call is queued as for a native opening.
- **Sampler materialization with no executor.** Dispatcher forwards `Sampler.CreateSession` with
  the spec and `execution_identity` and without `executor`, `expected_incarnation_uid` or
  `execution_principal` (addition: the sampler request drops them; attachments arrive separately).
  The sampler stores the conversation, composes the effective prompt and answers `ACCEPTED`. It
  opens no executor stream and prepares no workspace, which its lazy acquisition already allows.
  Central openings adopt the
  [native opening outcome contract](../runner_gateway/dispatcher/SPEC.md#native-opening-outcomes-and-dependent-commands):
  `INVALID_ARGUMENT` or `ALREADY_EXISTS` is `FAILED` with queued commands `session_opening_rejected`;
  `UNAVAILABLE` or a deadline retries under the same ID; anything else is `UNKNOWN` with commands
  held `session_opening_unconfirmed`. Addition: Dispatcher applies that contract to central owners.
- **Owner unavailable.** The Session exists and queues commands. There is nothing to wake, so no
  wake assessment is ever recorded for a `central_sampler` Session.
- **Observable progress.** `GetSession` and `WatchDispatches` expose `materialization`; the
  sampler's `GetSession.attached` shows `harness_state` and an unset `setup_state`; `FollowEvents`
  and History `FollowSession` show turns. History's sampler ingress accepts an announcement with no
  executor and binds the sampler as writer with the Session's Identity as execution identity and no
  Sandbox (addition: History ingress, a Sandbox-less variant of its Identity writer binding).
  `FindSessions` by Sandbox simply does not list it; presentation is
  [`SESSION_HISTORY_PRESENTATION`](task_dag.md#session_history_presentation--represent-sessions-without-a-required-sandbox).
- **`setup_script` and `cwd` are rejected at creation, not deferred.** Both are facts about an
  Environment and move to the attachment: section 2 carries `cwd`; a setup operation, its scope and
  its once-only no-replay rule are
  [`SESSION_ENVIRONMENT_SETUP_DESIGN`](task_dag.md#session_environment_setup_design--choose-where-setup-belongs).
  Nothing runs setup on a later attachment implicitly. Delegation that prepares a child workspace
  through Session setup today keeps working through an explicitly authorized attachment-scoped setup
  once that decision lands; until then a migrated V1 executor attachment keeps its durable setup
  attempt marker, so a completed or uncertain setup is never repeated.
- **Environment-dependent instructions.** The frozen `<environment_context>` renders the date and
  "no environment attached"; cwd and shell render as an update when an attachment is first used,
  as the sampler already does for a late-learned shell. `platform_instructions.j2` stops asserting
  a bound Sandbox (content change only). The caller's `spec.instructions` stay frozen whatever they
  say about environments.
- **Commands.** `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn` and `Compact`
  route to the sampler as today, authorized by the Session's recorded Identity incarnation. Code-mode
  `tools` work with zero Environments as the delegating actor for `ci-daily`. An executor tool
  (`exec_command`, `apply_patch`, `view_image`, `write_stdin`) with no attachment rejects the cell's
  promise before dispatch with a typed `NoAttachedEnvironment` detail and
  `request_outcome: "not_dispatched"` (addition: sampler); the cell continues.

### Worked example

```text
app → Dispatcher.CreateSession(session_id=S, identity="ci-daily",
        spec{placement: central_sampler, instructions: "Review the last 24 h of CI latency…"})
      ← Session{identity: ci-daily, materialization: WAITING}      // sampler Pod rolling
app → Dispatcher.SubmitInput(S, command_id=c1, "Start.")
      ← Dispatch{QUEUED_IN_DISPATCHER}
      … sampler back: materialization ACCEPTED, c1 delivered, turn 1 runs
cell → tools.buildbuddy_invocations(...)      // request_action as ci-daily, no Environment
cell → exec_command({cmd: "git log"})         // rejected: NoAttachedEnvironment, not_dispatched
agent → attaches ci-build (section 2) and retries
operator → History.FollowSession(S)           // writer sampler, identity ci-daily, no Sandbox
```

A caller that retries `CreateSession` with the same bytes gets `S` back; a changed briefing is a new
UUID. Deleting the sampler Pod between commit and materialization leaves `S` `WAITING` with `c1`
queued, not failed. A caller naming `identity: "ops-admin"` without a `create_session` grant on
`boundIdentity: ops-admin` is `PERMISSION_DENIED` before any row exists.

**Question for the operator:** confirm A; the rejection of `setup_script` and `cwd` at creation;
and that `create_session` with target `boundIdentity: I` is the grant for an Identity caller to
create a zero-Environment Session as I, which widens that operation's reading from "on an
environment bound to I" to "running as I".

## 2. `SESSION_ATTACHMENT_CONTRACT`: Environments a Session may use

### Alternatives

- **A. Dispatcher-owned desired attachments, sampler-applied, Gateway-fenced.** Attachments are
  rows in Dispatcher PostgreSQL beside the Session, changed by `AttachEnvironment` and
  `DetachEnvironment` on `SessionDispatcher`, pushed to the sampler as the Session's current
  attachment revision, and enforced per executor call by the Gateway's existing UID and incarnation
  check plus the Identity check.
- **B. Sampler-owned attachments.** The sampler's database is the authority and Dispatcher only
  projects it. Keeps the write near its user, but puts a second writable Session fact outside the
  directory the operator chose as the live Session authority, and admission authorization would run
  in the sampler, which holds no policy reader today.
- **C. Attachments as Environment-side state.** A `Sandbox.status`-style list of attached Sessions.
  Kubernetes is authoritative for policy, not for live Session control; a Sandbox cannot record a
  relationship to a Session that outlives it, and deletion races with the fence Dispatcher owns.

**Recommendation: A.** One placement authority, one transition record, and the same deletion
serialization Dispatcher already has per Sandbox UID.

### Contract

Native placement is unchanged: `environment_hosted` names one required exact Environment, and
`AttachEnvironment` or `DetachEnvironment` on such a Session is `FAILED_PRECONDITION`
`native_placement`. A `central_sampler` Session has zero or more attachments:

```text
Attachment {
  attachment_id: UUID       // caller-chosen; idempotency key of the attach
  name: "build"             // Session-local; unique among live attachments
  target: ExactEnvironment{name: ci-build, uid: e1}
  cwd: "/workspace/ducktape"   // optional; the executor's default otherwise
  desired: ATTACHED | DETACHED
  observed: AVAILABLE | SUSPENDED | UNREACHABLE | DELETED | IDENTITY_MISMATCH   // last observation
  revision: 7               // the Session's attachment revision at this row's last commit
}
```

- **Attach.** `AttachEnvironment(session_id, attachment_id, name, target, cwd?, expected_revision?)`
  on `SessionDispatcher` (addition). Admission runs in one transaction under the Session row lock
  and the Sandbox UID lock cleanup uses: the Session is `central_sampler`; the target UID is live and
  not tombstoned (the existing
  [deletion fence](../runner_gateway/dispatcher/SPEC.md#deletion-admission-fence) applies); the
  Sandbox's `spec.identity` resolves to the Identity whose name and UID the Session recorded
  (addition: the shared gate `SESSION_IDENTITY_ENFORCEMENT` builds); the name is free. The commit
  increments the Session's attachment revision and writes the transition row below. A retry with the
  same `attachment_id` and bytes returns the row; a conflicting reuse is `ALREADY_EXISTS`.
- **Who may attach.** The `environment` rule's `create_session` operation on the target (by name or
  `boundIdentity: ci-daily`), evaluated for the effective caller, which must be the Session's
  Identity or a privileged caller. Attachment is not a grant: it records that the Session may use
  the Environment it is already entitled to by sharing its Identity; it adds nothing to either.
- **Detach.** `DetachEnvironment(session_id, attachment_id, expected_revision?)` needs only write
  authority on the Session (its Identity or a privileged caller): it narrows. It commits
  `desired: DETACHED`, increments the revision and keeps the row. Reusing the name later is a new
  attachment identity.
- **Inspect.** `GetSession` and `ListSessions` project live attachments (name, target, desired,
  observed, revision) under the Session's read authority; Environment details beyond that need the
  rule's `read` operation on the Environment, as today.
- **Execute.** Executor tools take `attachment: "build"`. Omitted, it selects the sole live
  attachment; with zero or several it is rejected before dispatch with a typed detail listing the
  names. The sampler carries the attachment ID, the Session's current revision and the target UID on
  session admission and every execution operation; the Gateway refuses a call whose Sandbox UID is
  not the target, whose incarnation is not the fenced one, or whose current `WorkloadCredential`
  Identity is not the Session's (addition: the Gateway's Identity comparison). Acting in a bound
  workload is implied for its Identity, so execution needs no further grant.
- **Wake.** Nothing wakes an attachment implicitly: not a tool call, not an inbox notice. A tool call
  against a suspended target gets the existing typed `ExecutionEnvironmentFailure`
  (`STATE_SUSPENDED`, `not_dispatched`). The agent may call `ResumeEnvironment` on
  `EnvironmentService` when its Identity holds the rule's `resume` operation on that target
  (`ENVIRONMENT_LIFECYCLE_RULES`), then retry.
- **Desired versus observed.** Desired state changes only through the two operations. Observed
  state is reported, never chosen: the sampler records the Gateway's typed outcome of its last
  executor call per attachment, Dispatcher cleanup sets `DELETED` when it tombstones the target UID,
  and an Identity rebind of the Environment sets `IDENTITY_MISMATCH` and fails that attachment's
  work `execution_identity_changed` rather than re-attributing it.
- **Concurrent attach and detach, replacement, unavailable targets.** `expected_revision` makes a
  change conditional (`ABORTED` on mismatch); without it, operations serialize on the Session row.
  A same-name Environment with a new UID is never adopted: the old attachment is `DELETED` and the
  agent attaches the new one under a new attachment ID. An unreachable or suspended target is an
  observed state, not a desired change; nothing retargets work.
- **In-flight effects.** Detach commits before the sampler learns of it; the sampler refuses new
  executor calls at a lower revision and settles in-flight ones as observed or unknown, abandoning
  handles as its journal already does. Detach never claims an effect was undone, and the
  Environment's `cwd` and processes are left as they are.
- **Setup and cwd.** `cwd` is an attachment fact rendered into the next `<environment_context>`
  update. No setup runs at attach; if `SESSION_ENVIRONMENT_SETUP_DESIGN` selects an attachment-scoped
  operation, it keys on `attachment_id` and runs at most once per attachment under a durable attempt
  marker.
- **Transition record.** Dispatcher's `session_attachment_transition` table (addition) holds one
  row per commit: Session, revision, attachment ID and name, target name and UID, the Identity name
  and UID verified at admission, desired state, actor and effective caller, commit time. It is the
  recoverable source for
  [`SESSION_ATTACHMENT_HISTORY`](task_dag.md#session_attachment_history--record-attachment-transitions-in-history),
  which delivers it to History later; until then History shows no attachment provenance for central
  Sessions, and says so rather than showing one Sandbox.
- **Migration.** Each `central_sampler_v1` row becomes `central_sampler` with one attachment named
  `executor` at revision 1, target its former `executor_environment`, keeping the sampler's setup
  attempt marker and History's existing Sandbox association. Session IDs, conversations, commands
  and inbox data are untouched.

### Worked example

```text
agent  → Dispatcher.AttachEnvironment(S, attachment_id=a1, name="build",
           target={ci-build, e1}, cwd="/workspace/ducktape")
         ← Attachment{observed: AVAILABLE, revision: 1}        // ci-build bound to ci-daily
cell   → exec_command({attachment: "build", cmd: "bazel test //..."})   // Gateway: UID e1,
                                                                        // incarnation, identity OK
… ci-build idle-suspends …
cell   → exec_command({attachment: "build", cmd: "git status"})
         ← rejected: ExecutionEnvironmentFailure STATE_SUSPENDED, not_dispatched
agent  → EnvironmentService.ResumeEnvironment(ci-build, e1)     // rule: resume on boundIdentity ci-daily
agent  → Dispatcher.DetachEnvironment(S, a1, expected_revision=1)  ← revision 2
operator deletes ci-build; a new ci-build has UID e2
agent  → Dispatcher.AttachEnvironment(S, a2, name="build", target={ci-build, e1})
         ← FAILED_PRECONDITION                                   // e1 is tombstoned; e2 is not e1
other  → Dispatcher.AttachEnvironment(S2 of Identity ops, a3, target={ci-build, e2})
         ← PERMISSION_DENIED                                     // identity mismatch; no grant
```

**Question for the operator:** confirm A; that `create_session` on the target is the attach grant and
detach needs none beyond Session write authority; that an omitted attachment name selects the sole
live attachment; and that no tool call or notice wakes an attachment.

## 3. `SESSION_AND_INBOX_LIFETIME`: what outlives an Environment, and explicit deletion

### What is implemented

Dispatcher retains the Session row with `required_environment=DELETED` after its required
Environment is deleted (#14314). Notification keeps a UID-pinned inbox and its subscriptions live
when the destination Sandbox is missing or replaced: sources are still read and entries stored,
notice delivery waits for the exact original UID, and a same-name replacement never receives the
notice ([README](../notification_service/README.md)). Subscriptions carry their own `expires_at`,
so collection stops when nothing renews them. Explicit Session deletion does not exist.

### Alternatives

- **A. Retain until explicit deletion.** The inbox, its subscriptions and the Session row live until
  `DeleteSession`; a native Session whose Environment is gone keeps collecting until its subscriptions
  expire. Central Sessions deliver regardless of attachments.
- **B. Retire the inbox after a grace period** following Environment deletion for native Sessions.
  Picks a period nobody asked for and silently drops a standing agent's events.
- **C. Retire at Environment deletion** (the pre-2026-11 behavior). Loses unread messages and
  cancels standing subscriptions for a Session that is still addressable.

**Recommendation: A**, which is also the operator's stated direction.

### Contract

- **Delivery destination per Session kind.** For a native Session the notice destination stays the
  exact required Sandbox UID; after its deletion the notice is prepared and waits forever, which is
  honest, and the Session row says `required_environment=DELETED`. For a `central_sampler` Session
  the destination is the Session itself: the Notification worker resolves the Dispatcher record,
  requires no Sandbox, and submits the notice through `SubmitInput`, which Dispatcher routes to the
  sampler without executor readiness or wake, as it already does for central inputs (addition:
  Notification's destination check gains a Sandbox-less central branch; this is
  [`CENTRAL_SESSION_NOTIFICATIONS`](task_dag.md#central_session_notifications--inboxes-for-sessions-without-environments)).
  The inbox is owned by the Session's Identity incarnation, as the README already defines, with no
  `owner_namespace`/`owner_name` Sandbox; its quota row is the Identity. A notice never wakes an
  attachment (section 2).
- **Explicit deletion.** `DeleteSession(session_id)` on `SessionDispatcher` (addition), first for
  `central_sampler` Sessions and for native Sessions whose `required_environment` is `DELETED`;
  deleting a native Session on a live Environment waits for a Runtime per-Session stop, which
  `Quiesce` does not offer today. In one Dispatcher transaction under the Session row lock: refuse
  new admissions; fail queued commands proven unsent `session_deleted`; leave possibly forwarded
  ones `UNKNOWN`; mark the row `deleting`. Then `Sampler.DeleteSession` (addition): stop work, refuse
  commands, seal the journal and finish export; the sampler drops the conversation only after History
  acknowledges the seal, so deletion never truncates evidence. Dispatcher forgets the row once the
  owner confirms or is proven gone, with no marker, as selected: callers must not reuse the UUID,
  and History may reject a conflicting writer. Command receipts go with the row; their retention
  period is `SESSION_DELETION_FENCE`'s and is not chosen here.
- **What deletion does to the inbox and History.** Notification learns of it at its next claim, when
  Dispatcher answers `NOT_FOUND`: it retires the inbox and cancels its subscriptions, and retained
  entries and messages stay operator-readable until their existing 30-day payload retention expires
  (addition: the worker's `NOT_FOUND` branch; today an unprovable owner pauses the inbox). History
  is never deleted by Session deletion; `FollowSession` keeps serving, and the Session row's absence
  is not evidence that History was removed. Who may delete: privileged callers only in the first
  implementation; whether the `environment` rule gains a Session target for an Identity deleting its
  own Sessions is a question for `ENVIRONMENT_RULE_TYPE_DESIGN`, not chosen here.
- **Same-name Identity replacement.** The Session records `ci-daily` with UID `i1`. After the
  Identity is recreated as `i2`, every Identity-fenced path fails closed as implemented or settled:
  Dispatcher admits commands only from incarnation `i1`, delivery and wake fail
  `execution_identity_changed`, the sampler's delegated calls for `S` name `i1` and receivers refuse
  them, and the inbox owned by `i1` pauses on owner mismatch. The Session is stranded, not
  re-attributed; the replacement gets its own Sessions. Re-adopting a stranded Session under `i2` is
  Session identity reassignment, which stays unsupported.
- **Operator-only access while the Identity is absent.** Through the app's privileged credentials:
  `GetSession`, `ListSessions` and `ListDispatches` (the privileged backend read path), History reads
  and `FollowSession`, the inbox's retained entries through the app's operator facade, and
  `DeleteSession`. No send, wake, attach or inbox acknowledgement: nothing can act as the Identity,
  so the sampler's delegated call and a Notification send both fail closed. Deleting an Identity
  deletes no Session; a Session's Identity being reconciled back by Flux is a same-name replacement.

### Worked example

```text
ci-build (e1) is deleted while S has it attached and holds a GitHub PR subscription
  Dispatcher: attachment a1 observed DELETED; S stays addressable; no required_environment on S
  Notification: subscription keeps reading; the next PR event becomes a notice; SubmitInput(S)
    delivers to the sampler; the agent handles it with zero attachments
native Session N on ci-build (e1) is deleted with it
  Dispatcher: N retained, required_environment=DELETED, queued unsent work FAILED
  Notification: N's inbox and subscriptions stay live; the notice waits for e1 forever; the
    operator reads the entries from the app; subscriptions lapse at expires_at
operator → Dispatcher.DeleteSession(N)
  Dispatcher: no Runtime to stop; receipts and row removed; a retry of N's old CreateSession is a
    fresh admission and must not be sent
  Notification: next claim sees NOT_FOUND → inbox retired, subscriptions cancelled, entries
    readable by the operator until their payload retention expires
  History: FollowSession(N) unchanged
Identity ci-daily recreated (i1 → i2) while S exists
  app → SubmitInput(S) as the operator: QUEUED, then FAILED execution_identity_changed
  operator reads S's History and inbox; a new Session S' names ci-daily (i2)
```

**Question for the operator:** confirm A; the scope of the first `DeleteSession` (central Sessions
and native Sessions without a live Environment); that deletion retires the inbox and cancels
subscriptions while keeping entries operator-readable under their existing retention; and that a
stranded Session after Identity replacement stays operator-only rather than re-adopted.
