# Native session discovery: characterization gaps and deferred design

Status: **child Session discovery/projection deferred.** On 2026-10-16 the operator selected native
delegation without child Session discovery or controls; it is merged
([#10782](https://github.com/agentydragon/ducktape/pull/10782)): raw child frames remain recorded,
normalized output and turn/input bookkeeping remain root-only. The characterization in
[#10776](https://github.com/agentydragon/ducktape/pull/10776) is merged; remaining configurations
and independent-control experiments below are not gates for native delegation.
Discovery, if selected later, builds on the
[event ingress](../docs/runner_control.md#event-ingress): the runner pushes every Session it
declares, roots and discovered children alike, and the History Service's index is the record of
which exist. This holds the remaining characterization, proposed design and hardening candidates for
[`NATIVE_SUBAGENT_THREADS`](task_freezer.md#native_subagent_threads--as-linked-agentplane-threads).
The [hardening candidates](#deferred-hardening-candidates) are not a dispatch queue; the
[shared design](#proposed-shared-design) is not a shipped runner contract or a priority change.

## Characterization gaps

The latest [child Session evidence](../native/docs/child_sessions.md) records streaming, concurrent
siblings, nested ancestry, failure and restart assertions for the pinned harnesses. Keep the
remaining matrix configuration-specific; a read-only snapshot is not a live subscription, and
process restart is not transport reconnect.

The currently tested child scenarios live beside their assertions: [Claude](../native/claude/test_tools.py),
[Codex `multi_agent_v1`](../native/codex/test_tools.py), [Codex `multi_agent_v2`](../native/codex/test_v2_discovery.py),
and the [experimental Claude 2.1.292 comparison](../native/claude/x/remote_io/README.md#acceptance).
A child must be created by the real harness, not synthesized by an adapter or inferred from parent
prose. The test index distinguishes these cases from capabilities that remain unmeasured.

### Test method and evidence

Use the existing [scripted harness suite](../native/README.md): real pinned binaries, loopback
model endpoints, synthetic tool calls, and native input/output traces. This removes model delegation
choice from the test. It also avoids live credentials and recorded production conversations.

For each case:

1. Assert that delegation is offered in the model-facing tool roster. Explicitly script the native
   delegation call and prove it caused a child model request. A parent saying it delegated is not proof.
2. Gate progress on requests/events, not sleeps. For concurrent children, route exchanges by native
   identity or distinct synthetic task markers; do not assume sibling request ordering. Bound every
   scenario and settle all exchanges so teardown catches leaked background work.
3. Assert the native relationship and lifecycle evidence, separately from the model API transcript.
   Model request IDs help the test drive the harness; they are not necessarily available to the runner.
4. Retain `stdin.jsonl`, `stdout.jsonl`, and `stderr.jsonl` through the existing `native_logs` fixture
   in Bazel undeclared outputs. Record the commit (which fixes the binary pins), launch/initialize
   options, test target, and CI invocation when reporting a finding. Sanitize any published excerpts.
5. Distinguish **tested**, **unmeasured**, and **unsupported with evidence**. A missing event in a
   bounded observation is not proof that the harness can never provide it through another interface.
   Once a limitation is established, assert that behavior rather than skipping the case.

Tests inherit the `MODULE.bazel` pins; observations of another version are not evidence for the
pin. Codex `multi_agent_v1` and `multi_agent_v2` are separate configurations, not aliases. A first
failure is diagnostic evidence to inspect, not a reason to weaken an assertion into accepting either
behavior. Fix the script if it did not reach the intended stimulus; update a behavioral expectation
only against the actual native trace.

### Matrix

Each cell names what is still unmeasured. Scenarios already covered for both harnesses (spawn with
native identity and parent linkage, child completion and parent continuation) are not listed.

| ID  | Scenario and controlled stimulus                                        | Claude Code                                               | Codex app-server                                                         | Evidence needed before normalization                                                            |
| --- | ----------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| S2  | Child runs a shell tool and returns a unique result                     | Covered                                                   | Covered: v1/v2 automatic initial child attachment                        | Inputs, outputs and attribution asserted while the child is still active                        |
| S4  | Observe child prose with forwarding defaults and explicit opt-in        | Covered: default and opt-in; tested CLI opt-outs rejected | Covered: initial parent stream vs explicit loaded-child resume           | Claude forwards completed blocks, not child deltas; Codex streams deltas                        |
| S5  | Two children active together; finish in reverse spawn order             | Covered                                                   | Covered, v1 and v2                                                       | Arbitrary alternating sibling deltas remain outside the reverse-completion script               |
| S6  | Child tool fails, then child handles it successfully                    | Covered                                                   | Covered, v1 and v2                                                       | Exit 23 remains a tool failure; the child subsequently completes successfully                   |
| S7  | Child model request fails terminally; parent continues                  | Covered                                                   | Partial: v1/v2 failed leaf while ancestors remain active                 | Codex tests do not complete the held ancestor model exchanges after the failure                 |
| S8  | Interrupt child while its model exchange is held open                   | Planned: determine whether independent control exists     | Planned: exercise native child control                                   | Accepted control vs observed interruption; sibling/parent isolation; too-late behavior          |
| S9  | Parent completes, is interrupted, or exits while child is active        | Partial: parent completion and active-child crash         | Partial: active descendants across process crash                         | Parent interruption and alternative foreground/background configurations remain unmeasured      |
| S10 | Disconnect client while retaining the harness, then reconnect           | Planned: first establish supported transport              | Covered: v1/v2 WebSocket reconnect, gap history and child attachment     | Root resume alone does not subscribe to an existing child's live items                          |
| S11 | Restart harness and resume parent with completed and active children    | Covered for persisted completed history and active crash  | Covered: v1/v2 completed, failed and interrupted child reads             | Immediate-result crash durability and independent execution recovery are not established        |
| S12 | Child delegates to a grandchild within configured depth limits          | Covered: immediate-parent tool-call ancestry              | Partial: v1 depth 2; tested v2 fresh child omits collaboration tools     | Other v2 role/context configurations and explicit beyond-limit calls remain unmeasured          |
| S13 | Send child another input after it completes; attempt independent resume | Partial: independent resume                               | Planned                                                                  | Same session vs successor identity, supported addressing, and capability boundaries             |
| S14 | Repeat core scenarios with alternate subagent configuration             | Partial: forwarding flag; native background children      | Partial: v1/v2, concurrent children and explicit loaded-child attachment | Context forks, alternate roles, foreground mode and child input remain outside these assertions |

#### Claude communication surfaces

Do not treat every communication operation as another spawn, or a successful send receipt as proof
that the recipient consumed the message. Characterize these separately under the pinned tool roster:

- **`SendMessage`, active child:** gate the child's request, send another message, and
  determine when it is consumed and whether it interrupts work. Test child-to-parent and sibling
  delivery separately, including an unknown recipient. Do not infer delivery from a successful
  parent tool result.
- **`TaskOutput`:** running-task reads and `block=true` need separate gates. The pinned tool
  describes itself as deprecated; test its behavior without making it the proposed discovery API.
- **`TaskStop`:** stop a held-open child request; distinguish the tool receipt from an
  observed terminal notification and verify that parent and sibling work survive. Team shutdown
  messages, if advertised, are a separate cooperative protocol rather than equivalent cancellation.
- **`ListAgents` and configuration-specific team tools:** first establish availability,
  scope, and feature gates. An entry in a tool roster does not prove that it enumerates all native
  children. Keep cross-session/remote messaging outside these same-harness loopback scenarios.

#### Resume and child fate

Expand S10/S11 for both harnesses. Distinguish client reattachment to a live owner, runner restart
with retained journals, clean parent harness exit followed by native resume, and parent crash
followed by native resume. Record which processes and native storage survive each case; these are
not equivalent forms of resume.

For each applicable boundary, gate interruption with the child running (held at a model request or
tool), completed before its notification is consumed, completed after notification consumption,
failed, or cancelled. Include a pending follow-up message. Exercise conversational children and
non-conversation background tasks separately where supported; a task ID alone does not establish
a resumable conversation. Unsupported stimuli need evidence, not a fabricated terminal event.

Retain native traces from both process incarnations and answer:

- **Rediscovery:** does resume enumerate or redeclare children/tasks, with stable IDs and parent
  links? Is the inventory complete, historical, or only active? Can a previously known ID be queried?
- **Fate and timing:** is completion, failure, cancellation, continued execution, automatic restart,
  or unknown fate visible during handshake, after the first genuine input, through history, or only
  through an explicit query? Record each route separately. Parent model memory of a result is not
  independent runner-visible lifecycle evidence.
- **Delivery:** are terminal notifications replayed, omitted, or duplicated? Can the runner recover
  a result whose notification it missed? Does a queued message survive, disappear, or get delivered
  again, and what proves consumption rather than acceptance?
- **Execution:** does native resume restart child work, replace its ID, or replay a tool side effect?
  Use synthetic execution markers to distinguish retained history from fresh work. Do not send a
  follow-up merely to discover fate without recording that it can itself reactivate the child.

Use bounded phases: observe resume without input, then a genuine scripted parent input, then any
advertised status/history query. Negative observations apply only to that phase and route. Reuse
ordinary parent-resume fixtures, preserving native storage while starting a new harness process;
model-side gates establish the stimulus, but assertions about discoverability use native wire data.
Slice append-only traces at the process boundary so first-process notifications are not counted as
replay. Pin observed behavior per harness/configuration before advertising recovery capabilities.

Still open across transports: Claude task enumeration, RemoteIO server history hydration, and
recovery without native files ([RemoteIO plan](claude_remote_io.md)); Codex v1 crash and
active-child recovery, which the v2 results do not cover. Keep server-supplied history separate
from state recovered by the harness itself. No transport cutover is implied.

## Proposed shared design

The following ownership, identity, storage and API contracts are proposals, not assertions of
implemented behavior. Native controls and normalization remain gated by the characterization above.
Deterministic adapter/replay tests must supplement, not replace, real-binary characterization.

### Direction and scope

The proposal is a read-only projection of native harness evidence into logical Sessions. The
runner adapter interprets existing Codex and Claude messages; this does not add a
harness-specific declaration API. The runner owns the durable identity, ancestry, observations,
and availability; History indexes the announced Sessions, and the app may present related
Threads. Runner Gateway still routes authorized commands and lifecycle requests; it does not
discover children.

Scope includes every native Session the Agentplane-managed runner durably observes, including
an unparented root not opened through Agentplane (operator direction, 2026-10-10 PDT). This is
the runner's known inventory, not an exhaustive snapshot of every child hidden in a harness.
A child declaration starts no process, setup, or Sandbox and copies no parent command queue;
logical Sessions share the execution owner. Recognition starts read-only. A child name, parent
link, shared process, or Thread view grants neither independent control nor authorization.

### Identity and root-creation boundary

Keep the creation-request ID, native harness ID, and public Agentplane Session reference
distinct. Qualify native handles by execution owner, storage lineage, harness, handle kind,
and native scope. Resolve aliases and ancestry only from native evidence; conflicts or missing
continuity remain unresolved rather than merging or reparenting Sessions. A pending delegation
or pre-creation error is not a child; late evidence may declare one without inventing an earlier
running interval. These identity and record details are specified in the concrete contract below.

Root creation remains a separate unresolved design boundary. Today's Dispatcher
`CreateSession.session_id` and the harness's native ID are distinct facts; a service UUID does
not prove native conversation creation. Codex obtains its native ID from `thread/start`; Claude's
initial supported path retains the selected `--session-id`, while a no-input harness-minted ID
boundary still needs a pinned test. The first child-read slice need not change root allocation.
Do not add a synthetic prompt, silently bundle first input into today's API, or treat a request
ID as proof of exactly-once native creation. After an ambiguous launch failure, retry only when
positive native evidence establishes continuity; otherwise preserve an unknown outcome.

A future creation API may accept first input before native declaration when the harness needs it
to reveal its ID. Durably reserve the execution owner, native state, logical reference and attempt
journal, recording the creation intent and input command ID before dispatch. Pending means only an
attempt exists. Dispatch under the saved command ID without resubmitting after reply loss; bind
confirmed native identity without changing journal origins or losing earlier evidence, and preserve
an ambiguous outcome across the native creation crash window. Reusing an attempt key with changed specs is rejected. Keep creation,
observation/attachment and explicit resume separate; a new native conversation gets a new
declaration. Making native IDs public is an alternative requiring owner/harness scope, not a bare ID.
None of this selects a change to today's API; the later root section retains that decision gate.

### Read-only projection and gates

The concrete contract below is the detailed proposal for descriptors, harness-specific
observation and recovery, durable journaling/projection, ingress publication, and History/app
exposure. It is not a shipped protobuf or current runner capability. Keep raw native evidence,
configuration/version, and execution-owner incarnation with derived facts. Route child content
to child history and delegation/result evidence to parent history; unresolved frames stay
unattributed. Report coverage as identity-only, lifecycle/result-only, or partial transcript
only when the evidence supports it. Separate last known work outcome from current availability;
owner loss does not prove child completion, cancellation, or stopped execution.

The first slice remains read-only: do not resume, rerun, or use messaging as a status probe.
Independent child input/interruption, nested ancestry, complete enumeration, live child
subscriptions, broader identity reconciliation, and root crash recovery require their own
characterization and acceptance. Codex v2 and Claude 2.1.292 observations apply only to the
measured configurations; they do not authorize a general control surface or transport cutover.
The existing [matrix](#matrix), later implementation slices, and deferred hardening section
retain the specific test cases and unresolved gates.

### Concrete shared contract for both harnesses

This section chooses the read-only implementation direction. Names below are proposed semantic
fields/operations, not shipped protobufs. Keep the original native frame, version/configuration,
execution-owner incarnation, and evidence reference alongside every derived fact.

#### Records, identity scopes, and state

The runner persists one descriptor per logical entity with these independent components:

- **Identity:** opaque runner logical ID, execution owner, entity kind, qualified native handle,
  storage-lineage ID, and creation/discovery evidence. Native display names and paths are metadata,
  not lookup keys. Root creation can reserve an ID before native existence is confirmed.
- **Ancestry:** parent logical ID when resolved; otherwise the qualified native parent reference
  and an unresolved marker. Record delegation call IDs separately. Out-of-order parent discovery
  must not force reparenting to the execution owner or allocate a second child.
- **Availability:** current native load/attachment evidence (`loaded`, `not_loaded`, `unknown`,
  or explicitly disposed), plus observation time/incarnation. Track owner transport/process health
  separately; its loss invalidates claims of current observation, not recorded work outcomes.
- **Activities:** native turn ID where available, current work evidence, and historical outcome
  (`completed`, `interrupted`, `stopped`, `failed`, or unresolved). Preserve the exact native label
  and whether the fact is a live notification or reconstructed history. Never fabricate a turn ID,
  completion timestamp, or successful result from a load-state change.
- **Coverage:** what is known about identity enumeration, transcript content, lifecycle history,
  and freshness. A snapshot declares its query scope and completeness separately from its rows.
- **Capabilities:** separately record observed native operations and enabled public runner
  operations, with supported/unsupported/unknown states. A native tool callable by the parent model
  is not an independent runner operation. Read-only v1 enables inventory/follow only for children.

Qualified keys are `(execution_owner, storage_lineage, harness, handle_kind, native_id)` for
Codex threads. Claude task IDs additionally live under the positively identified parent native
session: `(execution_owner, storage_lineage, claude, parent_native_session_id, task_id)`.
An originating tool-use ID is a routing alias scoped to that parent session, not a substitute task
identity. Preserve many continuation-call aliases if evidenced. A reused/conflicting alias cannot
silently merge tasks; quarantine the attribution and expose a reconciliation error.

Runner storage replay restores these keys without contacting the harness. Native resume is a
separate operation: the observed root ID and retained state must establish continuity before new
child observations can join old mappings. Codex's restored child ID and parent relation provide
that evidence in the measured v2 cases; Claude's stopped notification can join the retained task ID
under its resumed parent even without a repeated tool-use ID. A clone of native state into another
execution owner does not silently merge histories. Missing or conflicting continuity leaves an
unresolved record, not guessed identity or automatic retry of old work.

#### Root creation and today's service IDs

The Session Dispatcher owns logical Session records and invokes the placement owner's `CreateSession`
through the Gateway proxy
after the Sandbox is ready. The Runner owns retained execution state, while History records a
Session after its ingress accepts Events. See the [Runner Gateway](../runner_gateway/README.md) and
[Session Dispatcher Session contract](../runner_gateway/dispatcher/README.md).

The native-root design does not yet choose how the Session Dispatcher and Runner should reserve a
root ID or bind it to confirmed native creation. The example sequence below predates the current
Dispatcher `CreateSession` path and is a proposal, not a current app path. Review the Session contract
and identity before implementing it; a harness-minted root ID would need to bind to the public
Session ID without treating a preselected `CreateSession.session_id` as proof of native creation.

The concrete root sequences are:

1. The app submits an authorized Sandbox request through Kubernetes and follows controller readiness.
2. The client calls Session Dispatcher `CreateSession` with a fresh UUID and its Session spec;
   the Dispatcher passes creation to the placement owner through the proxy. A future native-root
   contract must decide how that ID and launch intent reserve a logical root and execution owner.
   A retry carries the same UUID; whether the runner started it is read from retained runner state
   and confirmed by history as available. An announcement is not launch success.
3. **Codex:** initialize the app-server; send `thread/start`; commit the returned thread ID as
   the native binding before reporting root readiness. No client-selected native thread ID is
   required. First user input subsequently uses `turn/start` with its own command ID.
4. **Claude, initial supported path:** retain the adapter-selected UUID passed via `--session-id`.
   Persist that selection before launch, distinguish requested identity from observed native
   identity, and verify session-bearing traffic agrees. Initialization confirms transport readiness,
   not a separate harness-minted ID. A mismatch fails reconciliation rather than rewriting history.
5. Both return the same service-facing shape, with creation/readiness and native-confirmation
   status distinguishable. Input is admitted only to the appropriate root execution owner.

Omitting Claude's ID override is a later optional mode, gated by the no-input declaration timing
experiment described above. Clients need not choose native IDs, but a uniform client API does
not require both harnesses to mint them. Do not block the first read-only child slice on changing
root allocation. If first input is bundled with creation in a future API, journal it once as described
above; do not silently add such a field or a synthetic prompt to today's `CreateSession`.

#### Codex v2: spawn, observation, and recovery

For a root bound to native thread `T0` and runner logical root `R0`:

1. A parent model's `collaboration.spawn_agent` request remains an item in `R0`. Until native
   evidence exposes a child identity, it is only a pending delegation.
2. `subAgentActivity(kind=started, agentThreadId=T1, agentPath=...)` in `T0`'s stream declares
   logical child `R1`, kind `native_thread`, parent `R0`. Upsert by qualified `T1`, not by task name
   or path. The spawn item completing means creation activity completed, not child work completed.
3. Child-native frames bearing `threadId=T1` route to `R1`. Parent delegation/results remain in
   `R0`; relation events link the two. When the parent stream reports a child completion activity,
   emit a child-outcome observation citing that parent evidence, without attributing parent prose
   to the child or settling the parent's user command.
4. After process loss, keep `R1` and its last known outcomes; current observation becomes stale.
   On explicit root resume, verify `T0`, enumerate `thread/list(parentThreadId=T0)` through every
   page, and read `T1`'s metadata/history. Also inspect loaded enumeration as a different view.
5. Reconcile `R1` to `not_loaded`, retaining completed history or the observed interrupted turn.
   An unloaded `sessionId=T1` does not split the entity or remove its parent. Persist the historical
   turn key and provenance so repeated reads update the same activity instead of emitting a new
   interruption on every reconnect.

The measured direct-child query is not proof of complete arbitrary-depth ancestry. Recursive
scanning and ancestor queries need their own tests, cycle protection, and bounded pagination.
Do not perform child `thread/resume`, `followup_task`, or a model prompt just to populate inventory.
Live socket reconnection and subscribing to independently addressable children remain separate
acceptance cases; stdio process restart is not a substitute.

#### Claude: task declaration, routing, and recovery

For a root native session `C0` bound to `R0`:

1. A native `Agent` delegation stays in parent history. An agent-typed `task_started` with task
   ID `A1` declares `R1`, kind `agent_task`, under `R0`; record originating tool-use ID `U1` as
   an alias. Do not declare shell jobs or unknown task kinds as conversational children.
2. Forwarded content carrying `parent_tool_use_id=U1` routes to `R1` even when its `session_id`
   equals `C0`. Keep the parent tool result as parent content and child-authored output as child
   content, both linked to their original evidence. Missing alias resolution stays explicitly
   unresolved until later evidence permits attribution; never default it to root-authored text.
3. `task_notification(task_id=A1, status=...)` updates child work outcome. This does not close
   `R1` forever or prove it can accept independent input. On 2.1.252, measured `SendMessage`
   continuation is parent-mediated native behavior, not permission to expose a child input API.
4. On a 2.1.292 native-file resume, an automatic stopped-task notification joins `R1` using `A1`
   and resumed `C0`, even when `U1` is absent. Record native `stopped` rather than inventing a
   successful completion, cancellation request, or exact stop time. An unknown `A1` may be
   late-discovered from evidenced agent-task metadata without inventing its start interval.
5. Initialization responses, user-command results, and background-task results require separate
   correlation. An automatic empty result does not finish a newly admitted user input. Preserve
   uncertainty when the native frame lacks sufficient correlation; do not match by arrival order.
6. Completed-child recovery without a fresh notification leaves the saved outcome intact and
   current availability unknown. Parent transcript memory is not new lifecycle evidence. The
   unavailable 2.1.292 `TaskOutput` route must not become an automatic recovery query.

The declaration/routing model applies to both tested stdio and experimental RemoteIO captures;
production integration initially uses the existing transport and pin. Enabling 2.1.292 recovery
requires an explicit pin/capability rollout. RemoteIO input-origin metadata, receipt IDs, and
server history hydration have independent gaps and are not smuggled into this slice.

#### Durable ingestion, projection, and list/follow handoff

Use the existing runner storage and journal authority, not a new execution/message queue:

1. Durably append the raw native frame to the owner journal with its owner ID, incarnation, and
   source cursor before deriving externally visible facts. Recovery reprocesses any unprojected
   prefix. A truncated/uncommitted frame cannot support a published declaration.
2. In one durable registry transaction, upsert the descriptor/aliases/activity facts, advance the
   projection checkpoint and registry revision, and record destination-event work for the existing
   journal writer. Allocate the child's journal identity here; a repeated source frame finds the same
   descriptor. The publication work is an outbox for projection, not a harness-command queue.
3. Append derived events idempotently by
   `(owner evidence reference, destination logical ID, projection kind/index)`, then mark that work
   published. Journal append must support recovery
   of that key before acknowledging publication; an outbox alone does not prevent duplicates.
   A client may briefly see a declared child with no projected content yet, never a reference to
   nonexistent raw evidence. Child events carry qualified cross-log evidence references.
4. Store native activity/item IDs where available to distinguish repeated history hydration from
   new work. Source-frame deduplication alone cannot deduplicate a later query returning the same
   historical outcome. Keep newly received raw evidence while upserting the existing activity.
5. Publication to readers is the ingress push: each declared Session's journal goes out on its own
   `Push` call and resumes after the last acknowledgement, so no inventory revision or resnapshot is
   needed. Owner-local and child-local content cursors are never compared numerically.

Implementation must first verify that current storage can provide these atomic/idempotent seams;
if not, introduce the minimal journal primitive and crash tests before exposing child discovery.
Schema names are deliberately deferred, but the ordering and failure guarantees are not optional.

#### Exposure, controls, and acceptance boundaries

The History Service indexes each announced Session with its descriptor: kind, parent and owner
references, coverage, availability and capabilities. The runner mints a child's ID once, before its
first announcement, so no reader allocates public child addresses. The index retains records while
the runner is unavailable; their availability is the last the runner pushed. Ingress failure only
lags discovery: it never blocks the runner or requires an app database write.

Do not duplicate the parent's launch spec onto a child. The app may create linked Thread views from
the index; it does not create or supervise native children. Authorize child reads as the History
Service authorizes any Session's; knowing a native ID,
sharing a process, or receiving a harness message is not a new authorization grant. Native prompt,
path, and tool metadata are untrusted data, not instructions or proof of human origin.

Read-only child attach must not launch/resume the owner, rerun setup, or admit input/interruption.
Reject unsupported commands before durable admission with a capability-specific error. Historical
`canAcceptDirectInput` or a native parent-tool capability is not enough to enable a public operation;
command routing, authorization, receipts, retries, and sibling isolation each need acceptance tests.
Existing root controls and command IDs retain their semantics.

### Implementation slices and acceptance

1. **Evidence extraction and routing.** Keep the existing root launch path initially; pin its native
   identity-confirmation boundary and extract the characterized child evidence for each supported
   version/configuration. The bounded root-identity observations do not establish an
   input-free native identity-allocation API. Extend native wire types only for fields consumed; add
   adapter tests for declaration, aliases, attribution, and completion using the characterized native
   events. Retain unknown frames. Verify child events cannot settle parent commands or turns.
2. **Durable declaration and read-only child sessions.** Follow the existing-root discovery path;
   preserve service UUIDs and runner root IDs. Implement qualified evidence references and the
   registry/outbox/idempotent-journal seams above. Test root launch crashes before/after native
   binding without assuming exactly-once creation. Persist descriptors and attributed child streams;
   implement read-only
   list/attach. Crash-test every declaration/fan-out publication boundary and duplicate replay.
3. **Index and presentation.** The History Service's index and read API carry the descriptor. The
   app lists children from the index and presents linked Threads with coverage and availability
   indicators. No app database write is a prerequisite for declaring a native child.
4. **Broader recovery and control.** Add only the capabilities demonstrated by further tests:
   alternate subscriptions, native enumeration, independent controls, restart identity, nesting,
   and broader Codex v2 contexts. These are not prerequisites for honest read-only discovery.

Acceptance for the read-only slice must run both pinned harnesses through the runner with the
integration app unavailable. Prove that an ordinary parent action creates a discoverable child,
IDs and aliases agree, attributed content does not contaminate the parent, and child completion
does not destroy its identity. Claude's completed-child follow-up must reuse the child session.
Also cover late discovery, duplicate events, unknown native kinds, missing correlation, concurrent
siblings, attachment during discovery, owner loss, runner restart with retained storage, and explicit
rejection of child launch/control. Assert version-specific coverage without claiming a complete transcript merely because some
Codex child prose was observed. UI acceptance then proves presentation, not backend authority.

Concrete gates for this implementation (not satisfied by characterization tests alone):

- **Single-agent control:** for each supported harness, one creation key produces one reserved
  root and stable service/runner binding; changed-spec retries fail; ambiguous native launch is not
  repeated automatically. Attaching/following never becomes creation or resume by accident.
- **Codex v2 child:** a real spawn declares one child; duplicate activity frames and repeated
  relation/history queries retain that logical ID. After completed/active crash recovery, show
  `not_loaded` separately from completed/interrupted history despite the changed `sessionId`.
- **Claude child:** forwarded frames before and after task declaration resolve to one task; missing
  tool-use aliases do not contaminate parent output. On the characterized newer pin, automatic
  stopped-task and empty-result traffic during resume cannot settle user commands or initialization.
  On the older pin, do not advertise that recovery capability solely from the newer experiment.
- **Failure boundaries:** crash after raw append, registry commit, destination append, and before
  outbox acknowledgement. Replay produces no duplicate IDs or derived event keys and no dangling
  evidence references. Conflicting parent/alias mappings remain visible reconciliation errors.
- **Push handoff:** declare a root/child, restart the runner before and after its first
  announcement is acknowledged, and lose the ingress mid-push. The child reaches the index once,
  under one ID. Absence from partial native inventory never marks a child disposed.
- **Isolation:** the ingress refuses a child whose parent is bound to another writer. With the
  integration app unavailable, discovery still reaches the History Service; unauthorized reads and
  unsupported child commands fail before admission, and observation starts no native work.
- **Coverage honesty:** lost native state, missing terminal notification, stale owner connection,
  and unknown task kinds remain distinguishable. No reconstruction from prose is labeled native
  lifecycle evidence; no child is restarted to make its status observable.

## Deferred hardening candidates

These are not a live priority or acceptance obligations. Check current evidence before promoting a
concrete gap from the [freezer](task_freezer.md#harness-features-and-native-research). They extend the
characterization suite and its shared fixtures, not the deferred shared-session implementation, and
add no session authority or event store. The automatic current-versus-candidate upgrade lane is
excluded.

1. **Codex v2 live reconnect:** disconnect only the client during an active turn and after completion.
   Capture identity, history and lifecycle evidence on reconnect without submitting work as a status
   query.
2. **Input correlation and interrupt races:** script overlapping inputs and interleaved results for
   both harnesses; attribute each completion. Synchronize interrupt-before-start, active and completion
   boundaries. Reproduce and diagnose the observed Codex input-during-turn race.
3. **Root crash/resume and side-effect replay:** extend existing recovery tests with active/queued
   input fate and observable side-effect counts. Establish whether a completed tool effect repeats.
4. **Protocol contracts and diagnostics:** consolidate evidenced guarantees beside native APIs; retain
   raw traces, pending model exchanges and the failing boundary without masking the original failure.
5. **Duplicate-command retry:** lose a bridge reply, retry the same identity and assert execution count
   and retained result; do not add another queue.
6. **Synchronization and backpressure:** replace avoidable sleeps with frame/request barriers; exercise
   large frames and slow readers for integrity, ordering, cancellation and bounded teardown.
7. **Permissions and configuration:** correlate permission allow/deny, interruption and recovery;
   verify model/configuration changes against subsequent requests, including rejection and active turns.
8. **RemoteIO provenance and hydration:** distinguish input/control/background results and characterize
   history, epochs and cursors across reconnect/restart. Missing/unloaded tasks are not completed tasks;
   read-only recovery must not secretly reactivate them.

Follow the [evidence rules](#test-method-and-evidence) and record results in the
[matrix](#matrix) rather than a second one.
