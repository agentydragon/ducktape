# Runner delivery boundaries and harness identity

The operator selected Session UUIDs as the public command address on 2026-11-03.
The Dispatcher resolves placement and records the exact Environment UID for native execution.
The [Session address inventory](../plans/session_environment_model.md#session-address-inventory)
tracks remaining caller migrations. The [Runtime lifecycle](#runtime-lifecycle) and
[API transport](#api-transports-and-agent-workflows) decisions are approved. The harness store
and native-identity alternatives below remain proposals.

## Current delivery and inventory facts

- **Dispatcher** serves Sessions at `sessions:8080`. It owns logical Session reservation, the
  PostgreSQL command queue, guarded wake, and [per-Session delivery leases](../session_dispatcher/dispatcher/SPEC.md#session-delivery-lease).
  A durable queue receipt proves Dispatcher acceptance. Hosted commands reach HarnessHost through
  Environments; central commands reach the sampler. See the [Dispatcher README](../session_dispatcher/README.md).
- **Environments** serves the Runtime proxy at `environments:8082`. An exact Sessions service SVID
  authorizes hosted Session mutations. Every proxy replica observes Runtime independently and
  forwards with the exact Sandbox UID, workload Pod UID and Runtime boot ID. The Runtime fences
  admission and journals commands. The [proxy specification](../environments/runtimeproxy/SPEC.md)
  owns connection, identity and deletion behavior.
- **History** receives journals from the Runtime and the sampler. Runtime quiescence waits for
  History's acknowledgement of each sealed cursor. Dispatcher delivery and routing have no direct
  History dependency. Notification uses the committed journal to reconcile notice admission and
  message confirmation. See [History](../history_service/README.md) and
  [Notifications](../notification_service/README.md).

Dispatcher Session records own membership and placement, including after Environment deletion.
App metadata owns title and archive state. History summaries supply retained display state and
can lag the live owner. Dispatcher publishes activity from the Runtime and sampler watches, with
unknown states when observations go stale. App archive checks that activity; the Environment
diagnostics describe the current Runtime. See [Session activity](../session_dispatcher/dispatcher/SPEC.md#harnesshost-activity-observation)
and the [App contract](../app/README.md).

## Destination checks and authority

[Dispatcher admission](../session_dispatcher/dispatcher/SPEC.md#method-scope) checks direct and
delegated callers against the authoritative Session. Runtime mutation uses the Sessions actor and
the stored placement fence under the [proxy contract](../environments/runtimeproxy/SPEC.md#environment-runtime-proxy-authorization).
[Wake policy](../environments/lifecycle/SPEC.md#authority) separately governs automatic wake and
explicit resume. Notification subscription checks the recorded Session Identity and authorization
for the subscribed source. Incoming inbox messages and Session commands have their own permissions.

On 2026-10-15 the operator approved a public `(Sandbox UID, Session ID)` address and limited
service delegation to the App and Notification paths, with separate wake authorization. The
Session UUID decision superseded the public destination tuple. The retained exact placement still
fences wake and execution. The current grants and delegated identity checks are in the
[Dispatcher specification](../session_dispatcher/dispatcher/SPEC.md#caller-proof).

The early design compared three allocations of authorization checks:

1. Sender preflight followed by full policy checks at execution boundaries. This gives early
   errors and preserves source-specific knowledge. It duplicates reads and needs the same exact
   target across admission and execution to close races.
2. One admission authority for placement, Session membership and send policy, leaving
   subscription and source authorization with the source service. This needs trustworthy caller
   proof and a consistent decision when an Environment is unavailable.
3. Advisory preflight for user feedback and authoritative checks at admission and execution.
   A successful preflight does not authorize a later command.

The [Session command authority plan](../plans/session_send_authority.md) tracks sender grants,
revocation of queued work, authority after placement changes, and inbox permissions when the
original Environment disappears. Any new placement locator or generation must preserve the
exact execution fence across suspension and same-name replacement. A placement fence does not
grant permission. Resume or retry of an uncertain command uses its original ID and payload under
the [receipt contract](../session_dispatcher/dispatcher/SPEC.md#possible-command-forwarding-and-deletion-outcomes).

## Delivery service and persistence alternatives

The operator selected mandatory queue-backed admission and Dispatcher PostgreSQL on 2026-11-02.
The database-free and queue-off alternatives are closed. The open question is whether the
queue-backed API should offer an explicit
[immediate-or-fail send](../plans/task_dag.md#dispatcher_persistence_model_design--decide-on-an-immediate-or-fail-send).

The earlier direct-admission proposal was online-only and did not wake a suspended Sandbox. It
would have put retry ownership on the caller until the Runtime journal admitted the command.
The selected queue persists the Session, exact placement, command ID and payload before queued
acceptance or automatic wake, and survives sender and Dispatcher restart. For an immediate-or-fail
contract, decide the return point, ordering and cancellation, and how a lost response preserves
unknown admission and same-ID reconciliation. Keep Runtime journal acceptance distinct from
harness execution. Loss of Runtime storage ends that Runtime's native state. Retained History
has a separate lifetime.

The [service database inventory](system_overview.md#database-inventory) and
[K3s test-tier proposal](#cnpg-free-k3s-integration-tier) cover independent database dependencies.

## API transports and agent workflows

**Approved (operator, 2026-10-15); adopted one service and interface at a time.** Use gRPC for
internal service APIs, a gRPC-Gateway-generated HTTP/JSON facade over the protobuf contracts, and an
ergonomic CLI for agent workflows; no hand-authored HTTP API beside a gRPC service. Watches and waits
are the facade's newline-delimited JSON server streams, including terminal stream-error envelopes,
not SSE. Full-duplex connections stay native gRPC. Resume cursors, reconnection and retention remain
application contracts, not guarantees supplied by the facade. The facade contract is in
[gRPC services](grpc_services.md#httpjson-facades). Keep authentication and authorization, stable
errors, streaming and reconnect, wait deadlines and cancellation behavior consistent across the
facade and CLI; a transport conversion must not weaken a service's existing permission checks.

Rejected for the facade:

- **Connect:** RPC-shaped URLs and binary-enveloped streams that `curl` and `jq` cannot read, and a
  second agent-facing path beside gRPC-Gateway. The parked PRs (#7079, #7083) saw an open stream
  yield no frames ([transport experiments](thread_view_sync.md#transport-experiments)).
- **gRPC-Web through Envoy:** a translation hop in production and in browser acceptance (same
  experiments).
- **Envoy `grpc_json_transcoder`:** a service's HTTP API would live in cluster edge config; the facade
  travels with the service.
- **SSE through a custom gateway marshaler:** a community workaround, not an upstream mode, which
  gives agents and browsers different stream formats.

Agent tooling should support both concise commands and composable JSON for listing Action groups and
Actions, inspecting an Action schema, reading the authenticated caller's own Action status/results,
waiting for completion, and subscribing to or cancelling GitHub PR notifications. The
[`agentplane` CLI](agent_cli.md) uses each service's generated client and HTTP facade as appropriate.

## Harness process, store and Session identity

Each Claude Session runs its own supervised native process, with `CLAUDE_CONFIG_DIR` in the
Session's state directory. Every Codex Session of a Runner is one thread of the Runner's one Codex
app-server ([`codexappserver/host.go`](../env/runtime/codexappserver/host.go)), whose `CODEX_HOME` is `<state_dir>/codex`.
The app-server's command line and `CODEX_HOME` are process-wide. `thread/start` and `thread/resume`
carry each Session's working directory, model, effort, developer instructions and a thread `config`
map, whose `shell_environment_policy.set` gives a thread's tools its Session's notification
destination. The Session record stores one Claude session ID or Codex thread ID, and a runner restart
creates a new process and resumes the native conversation from that store. See
[`session/`](../env/runtime/session), [`supervision/`](../env/runtime/supervision/),
[`retained/`](../env/runtime/internal/retained), and the adapters ([Claude](../env/runtime/internal/harnesses/claude/adapter.go),
[Codex](../env/runtime/internal/harnesses/codex/adapter.go)).

The shared app-server starts when a Codex Session first needs a harness and stays up until the
Runner stops or quiesces; it is not reaped when idle. Stopping is phased: every Session halts first
(its active turn interrupted), each Claude process stops, then the app-server stops once. Its frames
are assigned by thread ([`codex/router.go`](../env/runtime/internal/harnesses/codex/router.go)): a response to the Session
whose request it answers, a frame naming a thread to that thread's Session or, for a sub-agent, its
root's, and a frame naming none, like stderr, to every hosted Session. Thread creation is serialized,
so a thread naming itself before its `thread/start` response belongs to the one Session creating it.

The app-server has one cgroup and memory budget, so one crash or OOM ends the active turn of every
hosted Codex Session (`PROCESS_LOST`); each resumes its thread when it next needs a harness. That is
the cost of sharing. Measured with the pinned Codex 0.157.0 against a model endpoint that never
answers (2026-10-24), eight Sessions take 131 MiB idle and 159 MiB with every turn waiting on the
model in one app-server, against 258 and 299 MiB proportional set size (969 and 1101 MiB summed
RSS) in eight.

**Decision (operator, 2026-10-24):** the operator asked for at most one Codex app-server per Runner
(2026-10-17 PDT). The per-Session Codex homes kept before it are left behind, not migrated: "I'd say
we leave it behind. I don't have any running sandboxes right now." (17:38 UTC). A Codex Session
created before it fails to resume its thread.

`HarnessStarted` records `resumed` and a PID, not a unique process-incarnation ID. PID is useful
process-local diagnostic data, but may be reused and does not identify a spawn over time. The stored
`native_session_id` survives process restart; it is a Claude session ID or Codex thread ID, not the
Agentplane public Session ID. The public ID also keys the Runner directory, Dispatcher Session record and
History record. The Environment UID is internal placement evidence. Callers address the Session
by its public ID.

Keep three axes explicit in the design:

- **Process incarnation:** one unique identity per harness process spawn, useful for routing and
  liveness; it changes on process restart. A PID alone is not a stable identity.
- **Harness persisted data/store:** data reused by a later process, such as a Claude Session's
  configuration directory or the Runner's Codex home. The same path/data may outlive a process. Decide
  whether successive processes using the same store count as the same harness object, and what
  evidence distinguishes a resumed process from a new store lineage.
- **Native Session:** the native conversation identity within the harness/store, which may be scoped
  to that store rather than globally unique. Keep it distinct from Agentplane's public Session ID and
  any native child identity.

### Coherent harness-ownership alternatives

One coherent option is exactly one harness type and one persisted harness store per Runner, with its
process lifecycle coupled to the Runner. If that harness safely supports multiple independently
interactive Sessions, all Sessions in the Runner can route through it. This is the only single-harness
variant here that removes store-address ambiguity, but all Sessions share the store, raising isolation
and concurrency questions. It also gives up mixed Codex and Claude Sessions in one Sandbox/Runner;
compare per-Sandbox harness selection and homogeneous templates with multiple-harness flexibility.

The other coherent option allows multiple harness processes and stores per Runner, with each process
potentially serving multiple Sessions if the pinned harness supports it. Do not impose an arbitrary
same-harness-type rule. This preserves mixed Codex/Claude use but requires explicit process/store
identity and Session-to-store/process routing. Once infrastructure supports multiple processes and
stores, a same-type-only rule adds little architectural simplification; actual resource limits and
the harnesses' proven capabilities still constrain what can run. Restricting a deployment to one
later is a smaller step than adding identities and routing after committing to one; this modest
migration-flexibility advantage favors keeping the multi-harness option available, but does not
settle the choice.

Do not use a one-process-at-a-time, one-harness-type design that switches among per-Session stores as
a middle ground. It retains per-Session store addressing after restart while also forbidding mixed
harness types and coupling every Session's process availability; it combines the constraints of both
alternatives without the fixed-store routing simplification. A process restart always creates a new
incarnation; a persisted store can remain the same across it. Native child Sessions that are merely
discoverable or read-only do not establish that one process can host multiple independently
interactive Sessions. Use the separate Codex and Claude characterization evidence before evaluating
either alternative. These are design options, not implementation authorization.

**Earlier public address and process boundary (operator, 2026-10-15; address superseded above):** process
allocation and routing are internal to the runner. Neither a PID nor a process incarnation belongs
in a durable public address, and there is no public `create Session on process` operation or refusal.
The public destination is `(Sandbox UID, Agentplane Session ID)`, with store/native/process mapping
private to the runner; no public store field is needed.
Creating a Session can select its harness kind while the runner manages a default store per kind;
that is a candidate policy, not a settled one-versus-many store/process choice. An adapter may
arrange one Codex app-server process per store or the current Claude stream-json process per root
Session. Process instances may appear as diagnostics with hosted Sessions/status. At-most-one
process per store and shared-store concurrent safety remain open.

Current [`SessionSpec`](../proto/session.proto) selects native Claude or Codex through
`placement.environment_hosted.harness`, alongside its exact required environment. It still has no
store selector; the runner creates `CLAUDE_CONFIG_DIR` under the Session's runner-owned directory and
one `CODEX_HOME` for the Runner. No production in-repo caller currently chooses either directory. A private default-store
policy remains a candidate; future store-management needs can be considered separately without
putting a store ID in the current Session destination. Central sampler V1 has a separate placement
with one required executor environment; it does not share the native harness store contract.

The Runtime proxy checks namespace, name and the exact Sandbox UID before it forwards a native
call. A proposed runner-owned store ID names one durable lineage and is never
recycled, even after retirement; it is not a path or process. A public Session ID names a logical
Session, durably mapped to its selected private store and native Claude/Codex ID. Native IDs may
collide across stores. Define authorization binding, unavailable/suspended lookup and store movement
without promoting a same-name Sandbox replacement.

**Recommended creation contract for review, not an approved ID-allocation decision:** keep
client-chosen, globally unique public IDs for client-created root Sessions. Dispatcher already uses
Session ID for idempotency and checks that placement, spec and setup agree on a retry. The proposed
private binding adds the following steps. The runner
first durably reserves the public ID
and its chosen private store binding, then asks the adapter to create a native Session,
then durably records the harness-chosen native ID before reporting a completed opening. A queued
opening is only acceptance; it does not assert native creation. Store a stable public-to-native
mapping in runner-owned state, scoped by store, and reuse it after process or Runner restart. Do not
make a native ID, PID or process incarnation the public retry key.

This ordering does **not** make native creation exactly once: Codex may create a thread and the
process may die before the returned native ID is committed. Recovery must inspect durable native
store evidence or use a proven native idempotency mechanism before binding that thread. If neither
can establish which native Session belongs to the reservation, mark creation unresolved and stop
automatic native-create retries; expose that non-success state through the reviewed API and retain
the possibly orphaned native data for reconciliation. A lost response after the mapping
commit resolves by the same public ID. Claude's current adapter chooses a native ID before launch;
that narrower path does not solve the harness-chosen-ID crash window in general. Specify the
reservation/mapping format, native reconciliation proof, and failure status before implementation.
Today [`Session.startHarness`](../env/runtime/session/native.go) writes the native ID only after adapter handshake;
[`CodexAdapter`](../env/runtime/internal/harnesses/codex/adapter.go) starts a native thread during that handshake, and
[`ClaudeAdapter`](../env/runtime/internal/harnesses/claude/adapter.go) selects an ID before launch but also persists it afterward.
Persisting Claude's chosen ID before launch can narrow its case; Codex still needs a proven recovery
path for an ID assigned by the harness.

**Store and Session lifecycle proposal for review:** a store has a durable internal ID, harness type
and binding to the exact Sandbox UID; it survives process exit, Runner restart and Sandbox suspension.
The runner creates or reuses a store under its selected policy. Public store operations are needed
only if callers truly need to choose or manage stores; the ordinary API opens Sessions, sends input,
interrupts an exact turn and changes model/effort unless a harness proves an option is necessarily
shared. A process has only ephemeral start/health/hosted-Session status and runner-selected capacity; it is never the
API target for Session creation or control. The runner may restart or replace a process without
changing the store or public Session address. A dormant Session keeps its mapping and data for
runner-managed resume on input. Retiring a store stops new Sessions and requires an explicit policy
for existing ones; deleting its authored native files is a separate, explicitly authorized data operation, never a
side effect of stopping a Session/process, failed creation or Sandbox replacement. Moving/copying a
store across Sandbox UIDs needs a new binding and authorization; do not silently preserve the old
destination. Until shared-store concurrent writes are proven safe for a pinned adapter, serialize or
exclude them within that store. Exact create/retire/delete RPC shapes and retention policy remain
open.

Native subagent IDs remain harness-chosen. If a discovered child is exposed as a public Session,
durably register its native ID with store and parent public Session before publishing it, assigning
a stable runner-chosen public ID on first discovery and reusing it on replay/restart. Parentage, visibility in History/UI and independent control
are separate: a child may be visible and read-only even when the harness cannot accept direct input
for it. A child's Sandbox/store lineage does not automatically grant the parent's send permission.
Child Session projection is deferred; the selected native
delegation scope does not expose child Sessions or independent controls. This identity contract does not presume Claude root multiplexing from the bounded
single-root stream-json evidence or select one versus several processes per store.

When a runner is reachable, a missing public Session is a
terminal unknown-target result; no other store, process or same-name replacement is searched. When suspended
or unreachable, queued acceptance cannot prove live membership and keeps the exact tuple until
delivery. Loss of runner storage ends runner-owned state; retained History is not a native store or
mapping reconstruction source. An unknown delivery response is handled by the same command ID,
not by inventing another Session.

## Runtime lifecycle

**Approved direction (operator, 2026-10-17).** Callers address durable Sessions; the Runtime owns
harness startup, native conversation preparation, reconciliation and process replacement. Process
allocation stays internal. This decision does not select process/store counts, shared-store write
safety or the remaining native-ID creation contract above.

### Admission and execution

Dispatcher durably queues the exact destination, command ID and payload before acknowledging queued
acceptance or requesting authorized Sandbox wake. The Runtime validates membership and durably admits
input **before** starting or resuming its harness. Admission is not native readiness or confirmation
that the harness consumed the input. An unknown Session is rejected, never implicitly created.

Once admitted, work belongs to the Runtime despite caller disconnect. It ensures setup and native
conversation preparation, reconciles durable evidence, then delivers input. Concurrent inputs share
one necessary startup attempt and preserve Session input order. Startup must not hold the admission
lock across native I/O or prevent controls from being processed. Definitive startup failures become
journaled command failures with their concrete cause. Specify bounded transient retries and how
all affected admitted commands settle; do not leave them pending indefinitely. Ambiguous native
creation or delivery requires reconciliation before retry, not blind recreation or replay of tool
effects. Same command ID/payload returns the original admission; conflicting reuse is rejected.

### Runner protocol and callers

- The Runner API has no `ResumeSession` or `StopRunnerSession` RPC, executable stop command
  variant, or corresponding Dispatcher/client surface; the removed wire field numbers and names are
  reserved. Add no process kill/restart RPC.
- Keep `CreateSession` as an idempotent durable Session reservation with immutable creation
  configuration. Its acknowledgement does not prove a running harness or completed native creation;
  repeated creation confirms the reservation rather than acting as a resume command.
- Keep the `SubmitInput` request and admission response shapes. Return durable admission without
  waiting for startup; later startup/delivery outcomes belong in the journal.
- Keep `InterruptTurn` and its required target `turn_id`. For a new command, atomically check the
  active turn and journal admission; a mismatch returns `FAILED_PRECONDITION` without admission.
  A retry of an already admitted command returns its original admission before checking current turn
  state. Serialize native interrupt dispatch with turn transitions so it cannot affect a successor.
  If the target finishes naturally after admission, record that outcome. Interrupt never starts a
  harness or cancels other queued/admitted inputs. No Session-wide interrupt or pause is introduced.
- Preserve Sandbox `Quiesce`: refuse new execution, settle work, stop harnesses, seal journals
  and wait for ingress acknowledgement before reporting quiescence. Serialize admission against
  quiescing; a racing send is either owned by the runner or remains queued for retry/wake.
  Let running Session setup and Sandbox bootstrap scripts finish naturally before teardown
  completes, rather than cancelling them partway through initialization. Orderly runner shutdown
  also waits for them; unexpected process loss is recorded as interrupted and never silently rerun.

Keep the guarded turn interrupt control and distinct Sandbox suspend/resume controls. Sending to a
dormant Session uses runner-managed readiness; existing Sandbox wake authorization still applies.
The app calls the [Environment resume API](../environments/internal/service/SPEC.md#resume-and-ownership).
The Runtime owns recovery of retained native state.

### Provisional introspection model and Sandbox page

Expose a coherent runner snapshot of harness processes and Sessions for frontend introspection.
Include per-spawn process identity, PID, harness kind, process state and assigned Session IDs;
per-Session status includes its assigned process (if any), persisted native conversation ID (if
known), preparation/readiness, setup state, active turn and failure evidence. Store identity may
appear as diagnostic information. Commands continue to target public Sessions, never a process ID.

The initial Session states are `DORMANT`, `PREPARING`, `READY` and `FAILED`. `READY` means native
conversation creation/resume and reconciliation have completed and the process can serve that
Session; no turn need have started. `PREPARING` includes process assignment/startup and conversation
preparation. Assignment may therefore precede readiness. A running Codex app server does not make
all conversations in its data directory ready or assigned: retained conversations remain dormant
until the runner prepares them. The default is lazy preparation when input needs it. Process status
uses `assigned_session_ids`, including preparing Sessions, rather than implying that all are ready.

Show process topology, Session mappings and per-Session status on the frontend **Sandbox page**.
Distinguish live status from unavailable status when suspended or disconnected; reading status must
not wake a Sandbox. Identify any retained snapshot as stale. Status fields and enum shapes are a
provisional information model and may change; clients must not use them to authorize, route or
coordinate lifecycle operations. The runner remains authoritative at execution boundaries.

### Rollout and rollback

**Lifecycle rollout accepted (2026-10-17 PDT).** Implementation #10950, promotion #10961
and explicit Claude `bypassPermissions` #10965 are deployed. Flux Image Automation owns all service
and runner image pins; the temporary starts/wake hold is released. Deployed acceptance covered
Pod/VM context retention, dormant/ready status, retained staging history and volume identities,
and the two Claude tool cases after the permission change.

**Operator UI acceptance (2026-10-17 PDT):** the operator reports a basic Codex message worked,
then test-drove idle suspension after the five-minute interval and successfully sent a message to
wake the Sandbox. This is acceptance of the tested staging UI path; it does not establish a
separate VM UI drive.

Keep previous image revisions and a tested persistent-format rollback policy. Before new work is
accepted, revert the coordinated release if necessary. After new writes, reconcile/preserve them;
rollback must not overwrite new authored data or replay command side effects. No live data deletion
or Sandbox replacement is implicit in this rollout.

## Harness capability boundary

The [test capability index](../native/README.md#behavioral-coverage) and its linked modules
record what the pinned binaries do in scripted scenarios. Codex's test demonstrates two root
threads over one app-server. The pinned Claude Code 2.1.252 test shows that a candidate `session_id`
on an inbound `user` frame does not route to a concurrent second root: its lifecycle receipt names
the process's selected root, and it queues while that root is active. Separate Claude processes
with distinct workdirs and config
directories can make concurrent model requests with different model settings; interrupting one
process leaves the other's request usable. Different credentials, sharing one config directory
across processes, and concurrent multi-process crash recovery remain untested. The existing
single-session `--resume` tests cover restart of one root in a new process.

The operator reports a finding from the private Claude remote-control investigation: an
orchestrator daemon polls outbound HTTP for session-start work and launches each Session in its
own Claude process speaking RemoteIO, which the operator describes as essentially the existing
stream-json stdio protocol. This has not been reproduced as a runtime process-count test here.
It gives no process-sharing benefit to adopt that mode solely for multiplexing. Use one Claude
process per root Session as the present allocation baseline and allow Codex to use its
demonstrated shared app-server, both behind the same public Session interface.
Neither process policy belongs in the public address or requires one process per store; another
Claude mode could change the evidence later.

Neither result decides how many processes or stores a Runner should use, whether concurrent Sessions
may safely write one store, or which native children can be controlled. The approved public address
does not settle those internal allocation questions; these tests do not authorize runtime changes.

## Runner delivery acceptance without History

A focused delivery integration target can omit the History Service while retaining PostgreSQL for
the Session Dispatcher queue. The test owns a small real `SessionJournalIngress` endpoint implementing the
runner's journal push and acknowledgement protocol, drives the Session Dispatcher API, and waits
for the runner's journal receipt to arrive at that endpoint. Correlate the delivered command by
Session ID and command ID; verify ingress cursor acknowledgements and that Quiesce reaches
`QUIESCED` only after the exact sealed cursor is acknowledged. This exercises queued delivery,
runner journaling and the ingress flush contract without a History fold, archive read or Electric
projection. It is a no-History test tier, not a CNPG-free tier: the Dispatcher still uses its
PostgreSQL queue.

## CNPG-free K3s integration tier

The proposed CNPG-free K3s tier is a deliberately narrow integration environment for independently
usable, loosely coupled components. It is not a full Agentplane deployment or a replacement for
CNPG. Once a reviewed execution contract removes the tested paths' actual execution-database
dependencies, the tier should exercise only flows that can run without those dependencies. It may
omit Notifications, History, Electric, the CNPG operator and PostgreSQL; it does not need parity from
services it omits. Keep other stateful services and their data owners explicit rather than implying
that removing one Dispatcher database removes every database dependency.
This is separate from the no-History delivery test above: a flow that still uses the PostgreSQL-backed
Dispatcher queue is not part of a tier that omits PostgreSQL.

Cover Pod and VM runner paths with test ingress where needed. The K3s VM rendering uses KubeVirt
under emulation and a Bazel-built guest. Select independently usable flows and record their
remaining state owners under the [test-tier gate](../plans/task_dag.md#cnpg_free_k3s_integration_tier--add-a-narrow-database-independent-integration-tier).
