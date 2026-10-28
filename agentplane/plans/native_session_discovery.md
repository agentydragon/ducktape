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

### Direction

Let a harness create native children. Its runner adapter interprets the native protocol evidence
and declares those children as logical sessions. The runner durably owns their identities,
relationships, observations, and availability, and pushes each declared Session to its ingress like
any other; the History Service indexes and serves them, and the app presents them as related
Threads. The Runner Gateway takes no part in discovery: it routes commands and lifecycle requests,
and a read-only child receives neither.

Scope is every native session the runner observes in its container, not only children of Sessions
opened through Agentplane (operator, 2026-10-10 PDT): the container is Agentplane's, and whatever
runs in it got there through Agentplane transitively. A native root not opened through
Agentplane is declared like a child, as an unparented Session with no spec, and nothing
filters it out as foreign.

This does not require adding an Agentplane-specific declaration API to Claude Code or Codex. Their
existing messages are the declaration inputs. The adapter is responsible for translating those
messages into a small shared vocabulary without manufacturing capabilities the harness lacks.

A child declaration must not spawn another harness process, run setup, allocate a sandbox, or
copy the parent's command queue. Several logical sessions can share one harness process and its
native state. Recognition is initially read-only; independently controlling a child is a later,
evidence-gated capability.

### Vocabulary and ownership

Use **logical session** as the runner's tracked conversational entity, with an explicit kind
(`root_conversation`, `native_thread`, or `agent_task`). A Claude child may remain an agent task,
not an independently addressable conversation. The common name grants no control capability. Keep these concepts separate:

- **Execution owner:** the durable runner resource that launches and supervises the harness and
  retains its native state. Today this is coupled to a root session; with harness-minted root IDs it
  must be reservable before that root is declared. It is not the semantic parent of every descendant.
- **Logical session:** a root conversation or a discovered native child. It owns attributed
  conversation history, not necessarily a process, workspace, or independently usable control API.
- **Parent relationship:** the native delegation ancestry, supported by evidence. Preserve the
  spawning call as provenance; a later message from a sibling does not reparent the recipient.
- **Activity:** one observed period of work within a session. Finishing it does not delete or
  permanently close the session. Use native turn IDs where available; otherwise retain the native
  activity evidence without claiming an exact model-turn boundary.
- **Native background task:** a harness-specific category, not automatically a logical session.
  A shell job, file transfer, or remote task must not become a conversation merely because it emits
  a `task_started`-shaped event. V1 recognizes only evidenced agent/conversation types.
- **Thread:** the app's conversation view and operator annotations. It is not the session registry
  or another execution authority. Follow the existing [layering contract](../docs/thread_layering.md).

The runner's inventory means **all sessions it has durably learned about**, not necessarily all
children hidden inside the harness. Report the distinction. Current native enumeration coverage
is insufficient to advertise an exhaustive live-native snapshot.

### Identity and declaration

Use the same declaration model for roots and children. A root's Session ID is the UUID its client
chooses for the current `OpenSession` request (formerly supplied through the Gateway's
`CreateSession` wrapper); the runner mints a UUID for each child it discovers. Either way the ID is
the Session's public identity on the ingress, and the History Service's index learns it from the
announcement. The proposed harness-minted-root flow below separates requesting creation from
observing a native conversation. Persist an opaque Agentplane logical ID and its native binding before publication.
Use the canonical logical identity direction in the layering plan; do not require existing root
storage paths to be rekeyed here.

A session descriptor should carry:

- Logical session ID, execution-owner ID, and parent ID when established.
- Origin kind: explicitly opened root, discovered root, or harness-discovered child.
- Native harness kind and qualified native handle, including its identity scope.
- Discovery evidence and the spawning call's native identity when available.
- Observed activity/availability, transcript coverage, and supported access modes.
- Optional display metadata supplied by the harness, treated as untrusted content.

Native handle lookup needs a persisted namespace, not a bare `task_id` or `threadId`. Qualify it by
execution owner, harness kind, native handle kind, and native identity scope. Native storage lineage and process incarnation are separate: a PID or process restart does not
by itself establish or break identity continuity. A new storage lineage starts a new namespace;
retained state plus positive native resume evidence can preserve the existing one. Runner replay on retained storage
reuses its saved mapping; matching a newly launched harness to old children requires positive
continuity evidence. Neither PID reuse nor equal descriptions proves continuity.

Within that scope, repeated discovery upserts the same child. Alternative references such as
Claude's `task_id`, `agentId`, and delegation `tool_use_id` become evidence-backed aliases, not new
sessions. A continuation call can add an alias without changing ancestry. Conflicting mappings
must retain the native evidence and surface a reconciliation problem, not silently merge children.

Declare only when there is native evidence of an actual child identity. A tool-call start with no
returned child ID is a pending delegation in the parent's history, not yet a child session. An
error before creation must not leave a phantom child. If an ID is first observed at completion,
allow late discovery without inventing a previously observed running interval.

### The single-agent case: harness-minted root IDs

A single-agent run is the smallest declaration case: one execution owner, one root declaration,
no parent relationship. The harness is authoritative about the native conversation's identity and
existence; the caller requests creation rather than declaring that a conversation already exists.

Keep three identifiers distinct:

- **Creation request ID:** caller-chosen correlation/idempotency key. Retrying it retrieves the same
  creation attempt; it is not a native or logical session ID.
- **Native session ID:** minted by the harness and learned from native protocol evidence.
- **Agentplane session reference:** a stable public logical identity bound to the qualified native
  ID: the client-minted UUID for a created root, a runner-minted UUID for a discovered Session, so
  native scope/format changes do not rekey public history. Making the native ID itself public is a
  possible alternative, but it still needs harness/owner scope; a bare native ID is not a globally
  unique Agentplane reference.

The proposed flow is:

1. The client requests a new root with a spec and creation request ID, optionally including the
   first input with its own command ID. It does not need to supply a future session ID.
2. The runner durably reserves the execution owner, native state location, and creation intent.
   It may reserve the eventual logical ID and journal here, but reports **pending creation**, not
   an observed native session. Setup and launch failures belong to this attempt.
3. The adapter starts the harness and requests a new native conversation. Native evidence supplies
   its ID; the runner commits the binding and a root `SessionDeclared` observation with no parent.
4. The client receives the declared session reference and can list, attach, and use the proven root
   controls. Identity binding must not change the journal's origin or discard pre-declaration evidence.
5. Later turns and explicit native resume reuse that binding when continuity is established.
   Creating another native conversation is another declaration, not a relabeling of this session.

The two harnesses have different declaration timing:

- **Codex:** the existing adapter already obtains a harness-minted `thread.id` from `thread/start`.
  That reply can declare the root before the first `turn/start`. Today it is stored underneath a
  separately client-chosen runner session ID; the new creation flow removes that client-ID prerequisite.
- **Claude:** the existing runner currently generates a UUID and passes `--session-id` on fresh
  launch; the adapter's `handshake()` returns that preselected ID after `initialize`, not an ID minted
  by the handshake response. To exercise harness-minted identity, omit the fresh-launch override
  and bind the ID from an observed native
  session-bearing frame. Do not assume `initialize` announces it: the exact earliest reliable
  declaration boundary needs a pinned test, including the no-input case.

If the ID is not revealed until input starts, the API must not deadlock by requiring an attached
session before accepting the first input. Persist that input in the creation attempt's existing
journal, dispatch it using that command ID when the native transport is ready, then bind the resulting
session declaration without resubmitting the input.
The pending creation reference can serve launch progress and declaration evidence until attachment
is possible. A UI may show a pending conversation, but it must not report a native session as already
created. Do not insert a synthetic user prompt merely to force the harness to allocate an ID.

Creation idempotency is not native exactly-once execution. If the runner crashes after requesting a
native conversation but before durably recording its ID, a retry of the same creation request must
not blindly start another one. Recover through positively identified native state/protocol evidence
where supported; otherwise report an ambiguous creation outcome. A JSON-RPC request ID or a saved
creation intent alone does not prove the native operation is safe to repeat. Test that crash window
before promising automatic recovery. Changed specs under the same creation key are rejected.

This proposes separating **create**, **observe/attach**, and **explicit resume** in the runner/service
contract rather than overloading `Open(unknown_id, spec)` with a guessed future ID. Use the existing
command/journal machinery for the attempt and first input, not another execution queue. Retain the
existing root mappings and history; implementation should make an atomic contract cutover rather
than add a tolerant second interpretation of `Open`. The first child-discovery slice may keep today's
root creation path, but that is an explicit scope boundary, not the final identity model.

Root and child discovery then converge: both commit the same descriptor and declaration observation;
only their cause, parent linkage, and capabilities differ. A child declaration has native delegation
provenance rather than a client creation request, and does not allocate a new execution owner.

#### End-to-end: provision a sandbox and start its first session

One common client flow can work for either harness. The operation names below are illustrative,
not claims about the current API. The caller can be a UI backend, CLI, or another agent.

1. **App → Kubernetes: create a Sandbox.** The app acts as the requesting operator and submits the
   selected template, preset label and authorized workload configuration. The Sandbox Controller
   reconciles it; allocated is not yet ready, so wait for controller status to report a reachable
   runner and required initialization to succeed.
2. **Client → Runner Gateway: `StartSession`.** Supply that sandbox reference, a session-creation
   request ID, the harness/model/workspace/instructions spec, and optionally the initial input with
   its own command ID. The service authorizes access, resolves the runner, and forwards the request;
   it does not operate the harness itself.
3. **Runner: durable acceptance.** Reserve the execution owner, native state directory, logical
   session reference `S`, and journal the creation intent and any initial input before dispatch.
   Return `S` in a starting/pending state and expose startup progress. Retrying the creation request
   finds this attempt, not another launch. `S` is usable to track the attempt before it proves that
   a native conversation exists.
4. **Adapter ↔ harness: native creation.** Codex runs `initialize`, then `thread/start`, binds the
   returned `thread.id`, and submits the saved input through `turn/start`. Claude can initialize in
   stream-json mode, submit the saved input, and bind the ID from a session-bearing native frame.
   Alternatively its adapter can preselect the ID through `--session-id` and wait for native
   confirmation. These choices do not change the caller's flow or justify resending the input.
5. **Runner → ingress → client: declaration and events.** Commit the qualified native-ID
   binding and declare root `S` with no parent and the creation request as provenance; its
   announcement and journal go to the ingress. The client reads `S` from the History Service (or is
   the ingress itself) and sends proven root commands through the Runner Gateway; it need not address a
   Codex thread ID or Claude conversation ID. Startup acceptance, native existence, input
   confirmation, and work completion remain distinct facts.
6. **If the harness delegates later:** native evidence declares child `S2`, with parent `S` and the
   same execution owner. The runner opens a `Push` call for `S2` like any Session, so the child
   enters the History Service's index with its proven coverage/capabilities; Agentplane does not
   provision another sandbox or launch another process to recognize it.

Creation failure remains inspectable through the pending attempt. The ambiguous native-creation
crash window described above still applies; request deduplication does not make native creation or
first-input delivery exactly once. No app database record is required for runner-side declaration.

### Proposed observation flow

Names here are illustrative; implement them in the existing runner Event language, not a parallel
command/event API:

1. **Session declared:** upsert the descriptor from native creation or later discovery evidence.
2. **Activity observed:** report running, idle with a last-work outcome, or unknown. Carry the
   native turn/activity key when available and preserve the exact evidence for the inference.
3. **Session capabilities observed:** update transcript coverage and any proven access modes.
4. **Session availability changed:** record native loss, confirmed disposal, or verified recovery
   separately from a completed activity. Historical identity and history remain retained.

For Claude, a plausible sequence is:

```yaml
- observation: session_declared
  session_id: child-session
  execution_owner_id: root-session
  parent_session_id: root-session
  native_handle:
    kind: claude_agent
    id: native-agent-id
    scope: native-incarnation-scope
  provenance:
    spawning_tool_use_id: native-spawn-call
- observation: activity_observed
  session_id: child-session
  state: idle
  last_work_outcome: completed
- observation: activity_observed
  session_id: child-session
  state: running
  evidence_kind: native_continuation_activity
```

These are semantic examples, not claims that each transition has a dedicated native frame. In
particular, successful `SendMessage` alone cannot justify running or message-consumed state. The
adapter must use the relevant native activity/content evidence. Keep unknown state when that
boundary is not observable.

Route native frames before applying conversation logic. Otherwise a child's forwarded assistant
frame can mutate the parent's item state, active turn, or command receipts. Parent history keeps
the delegation tool and its result; child history gets child-attributed content. Do not replace
child history with the parent's summary or render a forwarded child answer as parent-authored prose.

Claude routing initially joins `parent_tool_use_id` to the discovered child mapping. Codex uses
native thread identities where those are actually supplied on the client-visible stream. A frame
may provide both parent-tool and child-state observations; derive both with separate attribution.
Unresolved frames remain raw evidence until attribution is known. Never guess the root as the
recipient of otherwise ambiguous child traffic. Bound any in-memory correlation buffer and retain
an explicit unresolved state when it cannot be resolved.

### Runner protocol and storage seams

Today [`proto/runner.proto`](../proto/runner.proto), [`Session`](../runner/session/session.go), and
[`retained.Record`](../runner/retained/retained.go) couples a session to a launch spec, native persistence, process,
and active turn, and `Attached`, the ingress announcement, carries that spec. The first
implementation must separate the logical-session descriptor from execution ownership.

Proposed public behavior:

- Extend `Attached` with the descriptor: kind, origin, execution owner, parent, availability and
  coverage. A discovered child's announcement carries no `SessionSpec`; it must not acquire a fake
  copy of the parent's, since its actual prompt/model/settings may differ or be unknown.
- Every declared Session gets its own journal and its own `Push` call, opened when it is declared
  and after any lost call, as the ingress contract already does for roots. Discovery therefore
  needs no inventory snapshot and no list/follow handoff: a declaration racing a restart is pushed
  when the runner resumes after the last acknowledgement. A stopped owner does not erase its
  historical children.
- The ingress accepts a parent only if it is bound to the same writer, so a Sandbox cannot attach
  children to another Sandbox's Session. An idle child stays open on its call; finishing its most
  recent task does not seal it. Describe owner-process health separately from child activity.
- Reject launch/setup/spec mutations and unsupported commands on discovered children before
  admission, on the control stream. Do not route a child command to the parent's stdin just
  because they share a process. Existing root command idempotency remains unchanged.

Prefer retaining one logical Event stream per child so the current one-Session/one-Thread view model
remains meaningful. This needs an explicit evidence and storage design before protocol rollout:

- Preserve a raw native frame once at its execution owner. Child-derived Events must cite the
  owner's qualified `EventOrigin`; today's `Event.source_sequences` only addresses the emitting
  source and cannot express this cross-session reference. Extend the evidence language atomically
  and update readers/importers rather than treating the parent's sequence as a child-local sequence.
- Each session retains its own replay cursor; cursors from parent and child are not comparable.
  Sibling arrival order at the runner is not a universal native causal order.
- Persist the identity mapping and relevant event routing before publication. Crashes between
  declaration, child creation, and content append must replay to one child and one derived fact.
  Today journals are per-session SQLite databases: an in-memory dispatch into two journals is not
  an atomic commit. Choose a shared transactional boundary or a replayable, idempotent projection
  from the owner's durable evidence before implementing fan-out. Do not add a second command queue.
- If using replayable projection, record a durable applied prefix and deduplicate each derived
  observation by its source evidence and target. Keep already-published origins stable on replay;
  reinterpreting history after an adapter upgrade needs an explicit policy, not silent regeneration.
- The owner's journal reaches the same ingress under the same writer, so the History Service holds
  the referenced owner evidence and Raw stays resolvable after Sandbox deletion; a reader resolves a
  cross-log reference only between Sessions of one writer.

These are required design gates, not a claim that the existing journal already supplies multi-session
transactions. The read-only first slice should settle them without refactoring command scheduling
or adding independently executable child sessions at the same time.

### Discovery, capability, and recovery boundaries

Treat coverage as at least identity-only, lifecycle/result-only, or partial attributed transcript.
Only advertise a complete transcript when the harness boundary actually supports that claim. Codex
starts with its proven collaboration evidence; separately subscribing to a native child, if required,
is still an open test. A runner must not synthesize missing child text from a parent wait result.

Describe native message/control routes separately from exposed runner commands. Claude's proven
parent-tool `SendMessage` route does not establish a direct driver-input API for children. Likewise,
`TaskOutput` retrieving a result does not prove general history enumeration. A capability becoming
visible does not bypass the History Service's read authorization or the Runner Gateway's command
authorization, or grant child callers another ServiceAccount's authority. Shared-process children are not isolation boundaries.

On owner transport/process loss, mark previously active children unknown/lost as observations of
availability; preserve their last known outcomes. Do not convert loss into successful completion or
claim remote/native execution stopped unless the harness proves it. On runner restart, reload the
retained inventory without launching anything; on explicit native resume, reconcile only what the
native protocol can establish. Absence from a partial snapshot is not proof of deletion.

The [resume/fate characterization](#resume-and-child-fate)
must distinguish live reattachment, runner replay, clean native resume, and crash recovery. For each
child, retain the last observed work outcome separately from current availability and the evidence
that supports reconciliation. Report whether fate can be recovered automatically, only after input,
or via an explicit query; a remembered parent summary is not a new child lifecycle observation.
Deduplicate replayed terminal notifications without hiding a genuinely new work episode. A missing
notification, an empty partial inventory, or a not-found response does not establish successful
completion or cancellation. Preserve unknown fate when the native protocol cannot resolve it.

Do not automatically rerun unresolved work or use a child-message operation as a status probe:
reactivation may execute work again. Characterize native resume's own automatic restart behavior
and side-effect replay before offering it as recovery. Read-only acceptance must cover retained
outcomes with unknown current availability; stronger fate reconciliation remains test-gated.

Independent child input, interruption, nested ancestry, complete enumeration, and broader identity
scope reconciliation remain gated by the matrix. The measured Codex v2 relation/history reads and
Claude 2.1.292 stopped-task recovery are admissible only for their characterized configurations. Do not enable parent-injected tool calls as a hidden
implementation of a supposedly independent child command.

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

The Session Dispatcher owns openings and invokes Runner `OpenSession` through the Gateway proxy
after the Sandbox is ready. The Runner owns retained execution state, while History records a
Session after its ingress accepts Events. See the [Runner Gateway](../runner_gateway/README.md) and
[Session Dispatcher opening contract](../runner_gateway/dispatcher/README.md).

The native-root design does not yet choose how the Session Dispatcher and Runner should reserve a
root ID or bind it to confirmed native creation. The example sequence below predates the current
Dispatcher `OpenSession` path and is a proposal, not a current app path. Review the opening contract
and identity before implementing it; a harness-minted root ID would need to bind to the public
Session ID without treating a preselected `OpenSession.session_id` as proof of native creation.

The concrete root sequences are:

1. The app submits an authorized Sandbox request through Kubernetes and follows controller readiness.
2. The client calls Session Dispatcher `OpenSession` with a fresh UUID and complete harness spec;
   the Dispatcher passes the request to Runner `OpenSession` through the proxy. A future native-root
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
above; do not silently add such a field or a synthetic prompt to today's `OpenSession`.

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
