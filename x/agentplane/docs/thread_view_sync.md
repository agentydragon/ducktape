# Thread view synchronization

Status: **proposed contract, not deployed.** This document owns the derived
app-to-browser read API. [Thread layering](thread_layering.md) owns identities,
runner durability, command semantics, and the distinction between conversation,
pending commands, and operational state. The schemas below select API shapes;
they are not checked-in executable protobuf definitions yet.

## Decisions

- Keep the runner's sole command queue and the app's lossless copy of its Events.
  Materialize a rebuildable conversation read model in PostgreSQL. Opening a page
  must not replay the entire Thread on either server or browser.
- Normal mode reads recent assembled segments, current controls, and pending-command
  summaries, then follows compact changes. Raw Events and large payloads are demand
  reads. Neither initial loading nor idle background work downloads full history.
- Use protobuf-defined unary RPCs and server streaming. No client or bidirectional
  stream is needed: commands are independent unary calls; subscriptions resume with
  an explicit cursor. A dropped connection does not cancel admitted work.
- Use one original runner cursor space. A projection checkpoint is a position in
  that space, not a new app Event number. Derived changes are explicitly not Events.
- Evaluate TanStack DB as the read-only normalized frontend store. The actual
  library spike supports same-collection atomicity; transport and React integration
  still need their own tests. Do not add a second mutable server-state cache.

The [measured short Thread](../debug/thread_load_20260915.md) had eight turns but
3,091 Events and approximately 1.79 MB of SSE. Completed text/reasoning contained
6,278 bytes. This motivates materialization, not just compression or virtualization.

## RPC transport and generation

Browsers need a browser-compatible transport, not a direct `grpc.aio` connection.
Preferred integration to validate: `@connectrpc/connect-web` with binary protobuf
and a Python Connect ASGI service mounted alongside FastAPI on the existing origin.
The same client supports [Connect and gRPC-Web](https://connectrpc.com/docs/web/choosing-a-protocol/).
Choose Connect wire transport for the ASGI integration; call it Connect, not native
gRPC. Unary and server-streaming service definitions remain ordinary protobuf RPCs.

[Connect Python](https://github.com/connectrpc/connect-py) supports ASGI and Google's
protobuf runtime with its `protobuf=google` plugin option. Reuse existing `_pb2`
types and Protobuf-ES messages; do not introduce another Python message runtime.
This is a candidate backed by upstream support, **not an executed Agentplane
transport validation**. If it fails the build/auth/streaming gate, evaluate
`grpc.aio` plus a standard gRPC-Web translator. Do not write a framing protocol.

Generation must use standard Bazel rules and pinned local plugins:

- Keep `@protobuf` message targets and Aspect `ts_proto_library`.
  [Protobuf-ES service descriptors](https://connectrpc.com/docs/web/generating-code/)
  are sufficient for Connect clients; no second TypeScript service generator.
- Evaluate [standard Python gRPC rules](https://rules-proto-grpc.com/en/latest/lang/python.html)
  or their [plugin extension mechanism](https://rules-proto-grpc.com/en/latest/custom_plugins.html)
  for the chosen service generator. A Connect server requires its Connect plugin;
  `grpc_python_plugin` alone generates a different server interface.
- The repository currently has a narrow service-codegen rule in
  <../../../devinfra/python/grpc.bzl>. Its historical `rules_python` conflict is not
  sufficient justification: current `MODULE.bazel` already uses `rules_python` 2
  and patches `rules_conda` for it. Recheck the selected standard rules in a build;
  record a concrete remaining blocker before proposing an exception. No new
  shell-driven codegen, remote Buf plugin service, checked-in stubs, or duplicate
  import-closure wiring. Retiring the existing workaround can be a separate PR.

Transport acceptance requires an actual Bazel-built browser client and Python
server: unary, incremental server messages before EOF, cancellation, terminal RPC
errors, cookies, auth expiry, and cursors above `2^53`. Then exercise the deployed
ingress: buffering, idle timeout, reconnect, and replica replacement. HTTP/2 support
at an ingress does not by itself prove gRPC-Web translation or native gRPC support.
Extend `//x/agentplane/app:test_thread_browser`, which already exercises a real
browser, app and PostgreSQL. Mount RPCs before the SPA fallback, and retain app
draining behavior for open streams.

Mounting an ASGI service does **not** inherit FastAPI route dependencies. Every RPC
must use the existing caller/session authorization boundary, including resource
access checks; middleware alone is not proof. Require same-origin requests, explicit
cookie/CSRF protection for mutations, no wildcard credentialed CORS, and structured
unauthenticated errors instead of HTML login redirects inside a stream. Bound stream
lifetime and recheck authorization so an expired/revoked login cannot read forever.
Cancellation releases listeners/transactions; it never issues an interrupt command.

## Schema ownership and service surface

Keep `protocol/{command,event,event_log}.proto` harness-neutral and unchanged by view
requirements. Add `app/thread_view.proto` for the derived segment/change types and
`app/thread_api.proto` for services and app request envelopes. Reuse generated
`Command`, `EventEntry`, `EventOrigin`, item/turn enums, and timestamps. The runner
must not import either app file. There is no product identity named Conversation.

```proto
service ThreadViewService {
  rpc GetView(GetViewRequest) returns (ViewSnapshot);
  rpc FollowView(FollowViewRequest) returns (stream ViewUpdate);
  rpc ListSegments(ListSegmentsRequest) returns (SegmentsPage);
  rpc ReadPayload(ReadPayloadRequest) returns (PayloadChunk);
}

service ThreadCommandService {
  rpc Submit(SubmitRequest) returns (EventEntry);
  rpc ListPendingCommands(ListPendingCommandsRequest) returns (CommandsPage);
  rpc GetCommands(GetCommandsRequest) returns (CommandLookup);
}

service ThreadEventsService {
  rpc ListEvents(ListEventsRequest) returns (EventsPage);
  rpc GetEvents(GetEventsRequest) returns (EventsByOrigin);
  rpc FollowEvents(FollowEventsRequest) returns (stream EventEntry);
}
```

`ThreadCommandService` covers submission and queries of both pending and settled
commands. Its reads share the view's materialized checkpoint; the service boundary
does not introduce an app-owned queue or another ordering.

| Method                | Request shape                                                                                                              | Response/contract                                                                                                                |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `GetView`             | `thread_id`, recent segment/byte limits, optional reading anchor and bounded surrounding window, bounded local command IDs | One consistent `ViewSnapshot`; both a tail and an old reading window when needed                                                 |
| `FollowView`          | `thread_id`, projection position                                                                                           | Contiguous committed change batches, or explicit rebootstrap requirement                                                         |
| `ListSegments`        | `thread_id`, epoch/source, exclusive before/after anchor or around anchor, limits, minimum processed cursor                | Bounded current segments, sampled position, stable next/previous boundaries; not offset pagination                               |
| `ReadPayload`         | Thread, immutable payload reference, byte offset/limit                                                                     | Exact bounded bytes, next offset, completeness/availability; no live checkpoint advancement                                      |
| `Submit`              | `thread_id`, exact generated `Command`                                                                                     | Exact runner `CommandAdmitted` EventEntry, only after app archival; not effect completion                                        |
| `ListPendingCommands` | Thread/epoch, exclusive admission anchor, limits, minimum processed cursor                                                 | Pending summaries sampled at a position; explicit continuation and total unresolved count                                        |
| `GetCommands`         | Thread, bounded IDs, minimum processed cursor                                                                              | Exact admitted Commands and outcome references, or `not_observed_through`; includes settled commands outside all visible windows |
| `ListEvents`          | Thread/source, exclusive original cursor, optional end/filter, count/byte limits                                           | Exact entries, scanned range, next page token and availability; filtered output is not a contiguous Event prefix                 |
| `GetEvents`           | Thread and bounded original `EventOrigin` references                                                                       | Exact evidence or per-reference availability, including outside loaded history                                                   |
| `FollowEvents`        | Thread/source plus shared `Follow`                                                                                         | Explicit opt-in, unfiltered original Event log; independent raw checkpoint                                                       |

All limits have server-enforced maxima. Page tokens are opaque, bound to Thread,
source/epoch, direction and filter; not authorization capabilities. Counts are
useful because responses can be partial. Empty pages distinguish exhaustion from a
scan/byte limit and an unavailable source. Paged reads represent values larger than
their byte budget by references. For oversized raw entries, chunk the serialized
`EventEntry` and decode it with the same generated schema after reassembly.
`FollowEvents` remains an exact-entry stream: if an entry exceeds its message limit,
terminate with typed `RESOURCE_EXHAUSTED` details identifying the original entry and
its chunk reference. Resume after that entry only once retrieved and verified; never
skip its cursor or turn the reference into a fabricated Event. Similarly, an
oversized `Submit` admission response reports the archived receipt reference; it is
not a rejection of the already admitted command. Validate this boundary in transport
tests, and choose normal unary limits to fit accepted Command sizes.

Operational inventory/runner status remains a separate authority. Existing live
inventory endpoints remain during this slice. A later RPC replacement can offer
unary reads and server-streaming snapshots, with its own version/staleness metadata;
it cannot borrow a Thread projection cursor. Sandbox CRUD, egress/action policies, Actions,
connections, consent, and settings are not converted in this design PR.

## Positions, segments, and payloads

The following notation describes typed messages and `oneof` alternatives, not a
parallel JSON protocol. `uint64` remains `bigint` in TypeScript.

```text
Position = { source_id, projection_epoch, through_cursor: uint64 }
Segment = {
  anchor_cursor: uint64, revision_cursor: uint64,
  content: oneof(ConfirmedInput, Item, Turn, Control, GroupBoundary, Diagnostic)
}
ViewSnapshot = {
  position, segments: SegmentsPage[], controls, pending: CommandsPage,
  requested_commands: CommandLookup
}
Changes = {
  source_id, projection_epoch, after_cursor, through_cursor,
  segments: SegmentChange[], commands: CommandChange[], controls,
  unresolved_count
}
ViewUpdate = oneof(Changes, RebootstrapRequired)
PayloadRef = {
  source_id, projection_epoch, owner_anchor, field, revision_cursor, byte_length
}
ReadPayloadRequest = {
  thread_id, target: oneof(PayloadRef, EventOrigin), offset: uint64, max_bytes
}
CommandSummary = {
  command_id, operation_kind, bounded_preview, admission_origin,
  outcome: oneof(Pending, EffectOrigin, FailedOrigin, NoopOrigin)
}
CommandLookupEntry = oneof(AdmittedCommandAndOutcome, NotObservedThrough)
RebootstrapRequired = {
  reason: oneof(UpdateCacheExpired, CatchupBudgetExceeded, ProjectionRebuilt)
}
```

- `source_id` is the original runner journal identity. A changed source is an
  integrity/recovery condition, not a routine cache reset.
  If the app has not observed that identity yet, return an explicit
  projection-not-ready error; do not invent a source for an empty view. A known
  empty source can legitimately have a zero checkpoint.
- `projection_epoch` identifies one coherent materialization/reducer generation.
  It is not an Event counter or a compatibility version. Rebuild publishes a new
  epoch atomically; clients rebootstrap rather than combine generations.
- `through_cursor` covers every original Event through that position, including
  omitted native traffic. Only successful atomic snapshot/change installation
  advances it. A segment revision says when that segment last changed, not which Events
  the browser has consumed.
- One segment begins at each conversation-bearing Event, keyed by its immutable
  original cursor within the Thread/source. Item/harness-message/turn IDs remain
  fields, not substitutes for source scope. Later item/turn changes update the same
  segment. Receipt/lifecycle boundaries get lightweight segments even when they
  have no normal-mode card; grouping across pages therefore remains deterministic.
- `ConfirmedInput` preserves the harness-confirmed text reference and **all**
  originating command IDs. It is anchored at confirmation, not submission.
  Item segments preserve kind, native ID, tool name, observed completion/result,
  bounded text preview, and exact argument/output/text references. Unknown or
  incomplete output is not converted into successful completion after a crash.
- Turn-start segments provide context; terminal outcome/diagnostics have a control
  segment anchored at the terminal Event, after any partial output. Render the
  outcome once, not again at the start header. An empty failed turn therefore remains
  visible. Historical model effects have their own control segments; current model
  state cannot replace them.
- Controls carry evidenced applied model, active-turn identity and observed harness
  state. Initial configuration is separately identified as configuration provenance.
  `Attached` may be ahead of the archive and must not seed these Event-derived values.

An outcome origin points to the existing shared Event, not a new independent
execution-status vocabulary: confirmed input, model effect, interrupted turn, or
command-caused harness exit. Command summaries are materialized from those facts.

Bound both segment count **and bytes**, including IDs, previews, command summaries and
diagnostics. Large user/assistant text is explicitly partial with a payload reference;
ordinary short completed text arrives assembled. Tool arguments/results are omitted
from collapsed initial cards. A pre-window active item need not pull its entire turn
into the snapshot; controls identify it and navigation can fetch its segment/context.
The requested recent-segment count counts visible anchors; bounded grouping context must
not let a burst of invisible admission boundaries crowd the conversation out entirely.

Payloads are immutable at a reference, scoped to the source and projection epoch.
A corrected rebuild cannot reuse an old derived payload's cache identity. Exact
raw references remain epoch-independent original Event identities, not derived
payload references. `ReadPayload` with an `EventOrigin` returns serialized shared
`EventEntry` bytes, including an oversized admitted Command; the target variant
determines the decoding type. UTF-8 byte offsets are not JavaScript string
indices; streaming text uses an incremental decoder. Store append chunks plus
version manifests/prefix lengths, not another full copy of growing text per token.
A final authoritative replacement has a new reference; do not append a correction
as though it were a suffix. Normal live batches may include bounded text append
patches with an expected prior revision/offset, followed by the new reference. If
the inline budget is exceeded, continue reporting the reference and completeness;
expanded views fetch bounded ranges on demand. An unopened tool output does not
stream its body to the browser. No per-token full-message replacements.

Unloaded, loading, loaded-empty, partial/streaming, failed fetch, and unavailable
payloads are distinct UI states. Loaded payload caches are immutable and separate
from mutable segment metadata; they never become another authority for item completion.

## Server materialization and replay

The projection is a deterministic fold of a **committed contiguous** archive prefix,
plus explicit immutable configuration provenance where Events lack an initial seed.
It preserves the existing normal/Raw grouping semantics without retaining native
payloads in each segment. Test parity with the current reducer's meaningful output;
do not preserve accidental duplicate completion presentation.

For each bounded batch `(H, K]`, a projector transaction locks the Thread projection
checkpoint, reads its next archived Events, updates segments/command indexes/controls,
records the derived change batch, and advances the checkpoint to `K`. Commit before
notification. Multiple app replicas can serve reads and submissions; a row lock or
fenced worker lease serializes projection writes. This is independent of the existing
Sandbox ingestion lease. Projection may lag archival; report the two positions
separately in debug/operational status.

Retain a bounded journal of **derived updates** for short reconnects. It is a
rebuildable cache, not another durable command queue or independent Event history.
Each batch records original interval endpoints, not an app sequence number. Read
transactions observe segments, controls and the checkpoint consistently. `LISTEN/NOTIFY`
only wakes durable rereads; notifications lost between replicas or over reconnect
cannot create gaps. Follow registers for wakeups before its last empty reread and
checks again after listener reconnection. Do not hold a DB transaction open for the
lifetime of a browser stream.

Publish empty-visible-change batches too: a run of only native Events still advances
coverage. Coalescing within a batch must retain every new historical anchor and
command/control effect, even when its final state supersedes an earlier intermediate
state. Exact intermediate streaming chronology remains in Raw.

Keep the committed batch boundaries on replay; every snapshot position is one of
those boundaries. A single source Event can affect many segments: if its update exceeds
the transport budget, require bounded rebootstrap rather than publish half an effect
or invent intermediate runner cursors. Large bodies remain referenced payloads.

Before replay, enforce limits on retained batches, bytes and catch-up work. If the
cursor expired or the budget would be exceeded, send `RebootstrapRequired` with a
typed reason, then close normally. A slow consumer gets bounded buffering and the
same resync path, not unbounded memory. A broken source, conflicting duplicate,
future cursor, or corrupt projection is an error, not an empty fresh snapshot.
Transport keepalives are not committed progress and have no invented cursor.

Rebuild off the archived prefix into a new epoch, catch up, and atomically select
that epoch. Reject/make explicit an incomplete archive; never skip corrupt Events.
An unsupported new observation must halt the affected projection with a diagnostic
until interpreted; exact evidence remains readable. Rebuild is background work,
not work charged to the first viewer. Update-journal expiry does not delete raw data.

## Snapshot, live stream, and command recovery

```mermaid
sequenceDiagram
    participant F as Browser
    participant A as App projection/archive
    participant R as Runner
    F->>A: GetView {thread:T, recent:50, local_ids:[C]}
    A-->>F: Snapshot {source:S, epoch:E, through:900, segments, pending, controls}
    F->>F: Atomically install snapshot and cursor 900
    F->>A: FollowView {T, S, E, after:900}
    A-->>F: Changes {after:900, through:940, segments, commands, controls}
    F->>A: Submit {T, Command C: ChangeModel(M)}
    A->>R: Same Command C
    R-->>A: Event 941 CommandAdmitted(C)
    A->>A: Archive admission
    A--xF: Admission response lost
    A-->>F: Changes {after:940, through:941, C admitted, applied_model unchanged}
    Note over R: Current turn continues. Another command can be submitted
    R-->>A: Event 970 ModelChanged(C, M)
    A-->>F: Changes {after:941, through:970, C effected, applied_model:M}
```

`Submit` never waits for native effect. A normal return proves archived admission;
a timeout/cancellation does not prove non-admission. Retain the exact local command
until matched against archived evidence. A submit receipt beyond the installed view
cursor can show saved confirmation but must not advance view coverage or applied
controls. Ordinary input, model change and interrupt share this rule; interrupt
continues to identify the intended turn and cannot drift onto a later turn.

`GetCommands` reconciles old **settled** commands as well as currently pending ones.
`not_observed_through:H` is explicitly not a NACK. If the runner is reachable, retry
the same immutable ID/payload against its journal; never mint a replacement ID to
resolve an ambiguous response. Payload conflict remains a conflict. After a tab
reload, local records that were reconciled can be removed without downloading all
history. A queued model change remains pending until `ModelChanged`, failed or noop.

Pending summaries have their own bounded page independent of conversation segments.
Keep unresolved count and controls always present, locally submitted IDs pinned,
and fetch further pending entries on demand. Live command summaries/outcomes update
loaded entries and counts even when their admission is outside the history window.
The count must not imply the visible first page is the whole queue. Large exact
Command payloads use the bounded payload mechanism; compare generated Commands on
reconciliation, not just summary text or IDs.

On a short reconnect, follow from the last installed position. Batches must start at
that position; duplicates require matching identity/content. Partial overlap or a
gap is not guessed through. Recover by rereading/rebootstrap; report conflicting
content as an integrity error. Connection loss leaves visible data marked stale.

On a long gap or epoch change, cancel the old subscription, increment the frontend
request generation, and get a new bounded snapshot, including the reading anchor
and local command IDs. Install its segments/controls/commands/position atomically, then
follow. Preserve drafts/disclosures/viewport position, not stale server truth. Old
generation callbacks cannot write into the new store. Unavailable anchors are
reported with a reason; the client does not silently jump to the tail.

```mermaid
sequenceDiagram
    participant F as Browser offline at 970
    participant A as App through 500000
    F->>A: FollowView {T, S, E, after:970}
    A-->>F: RebootstrapRequired {UpdateCacheExpired}
    F->>F: Cancel old generation. Retain draft and reading anchor 400
    F->>A: GetView {T, recent:50, around:400, local_ids:[C]}
    A-->>F: Snapshot {through:500000, tail, reading window, C settled, controls}
    F->>F: Atomic replacement without missed-token replay or jump to bottom
    F->>A: FollowView {T, S, E, after:500000}
    A-->>F: Changes {500000..500020}
```

## History and live-data races

History is keyset-paginated by immutable first-observed anchor, not timestamp or
offset. A new segment cannot be inserted behind an already processed anchor. Updated
old items keep their original anchor. Page edges include lightweight group/turn
context, not every item in a potentially enormous turn.

**Chosen consistency:** each page is a current short DB snapshot at `P`, at least
the request's `minimum_through`. Adjacent pages need not share a long-lived historic
DB snapshot; stable anchors prevent insert-induced holes. A page's segment revisions
are at most `P`. It does not advance the browser's live cursor.

The view stream includes compact changes for every affected segment, including those
outside loaded windows. The browser keeps a **bounded** recent change buffer, not all
those segments. This permits race-free hydration without a bidirectional subscription
or per-browser server-side window registry:

1. At installed cursor `H`, request the page with `minimum_through:H`; retain change
   batches from `H` while the request is outstanding.
2. A response at `P > current_cursor` waits until the live stream reaches `P`.
   It must not inject future state into an earlier snapshot.
3. If the browser is already at `K >= P`, merge the page plus buffered changes
   `(P, K]` for those segments in one transaction. Ignoring a late lower-revision
   segment is sufficient only if a newer complete segment is already loaded; an
   evicted segment may need patches based on the page, which is why the buffer exists.
4. If the needed buffer was evicted, a patch precondition fails, or the epoch/source
   changed, discard/retry hydration or rebootstrap bounded windows. Never declare a
   stale page current. Requests and buffered bytes have explicit limits.

New-segment changes contain a complete bounded segment. Patches for unloaded old
segments are buffered for pending hydration, not applied to fabricated empty items.
Page windows become observable only after suffix reconciliation; displaying an
unreconciled page as current would violate the checkpoint contract. If a minimum
cursor cannot be served before the request deadline, report projection lag rather
than return an older successful snapshot.

```mermaid
sequenceDiagram
    participant F as Browser at 1000
    participant A as App
    F->>A: ListSegments {before:400, min_through:1000, limit:50}
    A->>A: Read segments and checkpoint P=1010 consistently
    A-->>F: Changes {1000..1010}
    A-->>F: Changes {1010..1030, update old item I}
    A-->>F: Delayed SegmentsPage {through:1010, I, before:350}
    F->>F: Merge page + buffered 1010..1030 for its segments
    Note over F: Still through 1030. Preserve visible segment and pixel offset
    F->>A: ReadPayload {I.output, revision:1020, offset:0, limit:65536}
    A-->>F: Exact chunk at that reference without changing live cursor
```

Pending-page and command-lookup hydration obey the same position/generation rules.
For pending membership, live settlement removes an entry even if it raced the page;
new admissions have higher anchors and are found through live updates. Immutable
payload chunks need no segment overwrite: cache by reference and show them only while
that reference is selected, or explicitly label a historical revision.

Keep a bounded tail window and, while reading far back, a bounded window around the
viewport. Evict/refetch intervening history; do not accumulate a month of segments just
because the tab stayed open. Pin controls, pending-page metadata and local commands,
not the entire turn. Prepending preserves the visible segment plus pixel offset. New
output sticks to the bottom only if the user was already there. Virtualization
bounds mounted DOM independently of network/cache bounds.

## Raw and debug surface

Raw remains additive to the same conversation order and disclosures. Show source,
segment anchor/revision, installed projection cursor, applied command evidence and
separately reported archive/runner positions. Fetch exact frames and causal
`source_sequences` only when requested. Raw chronological entries retain original
cursor order even when an aggregate card spans interleaved streaming Events.

`ListEvents` reports the original range it scanned and any filter. An empty filtered
page is not proof no Events occurred. A direct evidence lookup can find Native
frames before the visible page and receipt/effect Events after its first anchor.
The unfiltered raw follower is an explicit debugging option, not a background
requirement for normal mode. Neither raw pagination nor direct lookup moves the
conversation checkpoint. A live Raw panel follows archive availability separately
from the potentially lagging projection.

Availability is typed: retained, not-yet-archived, unavailable/lost, and (only if
retention is later implemented) not-captured/expired. Transport failure is not empty
evidence. Raw authentication is no weaker than conversation authentication, and
raw payloads are not copied into routine telemetry or error messages.

Initial implementation retains **all** observed runner Events, native frames and
deltas. Optional app-wide/per-Thread retention is separate: define policy precedence,
runner/app scope, future capture versus deletion, absent-range explanations and
rebuild/recovery consequences before deleting anything. Final assembled text is not
a lossless replacement for partial output, crash boundaries or streaming chronology.

## Frontend ownership

One TanStack DB collection per open Thread owns a tagged union of segment, command,
control and coverage records. Commit each snapshot/change envelope with a single
collection transaction, so direct subscribers cannot see a new cursor with old
controls. Read-only custom synchronization owns writes; optimistic user mutations
cannot manufacture harness effects. Use selective queries per segment/queue/control.
Separate immutable payload/raw collections do not participate in live coverage.
Request generations and cancellation protect route changes and rebootstrap.

The test-only spike's focused Bazel tests passed collection atomicity, selective
subscriptions, generated `uint64` values above `2^53`, late-page regression, stale
generation rejection, evidence separation, eviction and mutation rejection:
[expanded test invocation](https://app.buildbuddy.io/invocation/0c593693-929f-4bab-a4ad-cb437efa60ff),
in [the test-only evaluation PR](https://github.com/agentydragon/ducktape/pull/7069).
This does **not** prove React render consistency, HTTP streaming, the page-buffer
algorithm above, or deployed performance. Validate those before production cutover.
If the library fails the required integration, use Redux Toolkit/RTK Query as the
alternative owner, not as another cache beside it.

Validate an entire batch before `begin`: the evaluated sync API has no public
rollback transaction. Await the commit receipt before marking initial load ready.
Automatic query-to-server pushdown is not established by the spike; the view manager
explicitly owns bounded requests, stream consumption and eviction.

Draft text, scroll/disclosure state and exact locally unconfirmed Commands remain
local state with distinct provenance. Do not persist authentication-independent
server caches across users; logout/identity change cancels subscriptions and clears
accessible cached data. React consumes the collection through the library's supported
external-store integration; no effect-based second copy of the entire Thread.

## Failure and acceptance matrix

RPC errors distinguish invalid input/ID conflict, authentication/authorization,
missing Thread, temporary unavailability/projection lag, exhausted resource budgets,
and integrity loss. A command rejected before admission has no runner Event;
post-admission failure has `CommandFailed`. A submit deadline is neither kind of
NACK. Retriable read errors preserve stale visible state and an actionable retry.
`RebootstrapRequired` is a typed sync transition, not an arbitrary transport error.

| Case                                                | Required observation                                                                       |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Short high-delta or month-long Thread               | Bounded first payload/work; assembled past text; no eventual full-history fetch            |
| Empty/new Thread; projector behind archive          | Valid empty snapshot or explicit projection-not-ready; never fake successful catch-up      |
| Native-only interval                                | Coverage advances without fabricated conversation segments                                 |
| Active pre-window item, huge turn/output            | Current controls intact; bounded segments/previews; exact demand-loaded content            |
| Model queued during Codex turn                      | Admission visible as pending; picker changes only on evidenced effect                      |
| Interrupt or command failure                        | Original target/cause and error shown; no fabricated interrupted/completed state           |
| Claude-coalesced input                              | Exact confirmed text and all origin command IDs, at native confirmation position           |
| Failed turn without output                          | Visible diagnostic without Raw and without duplicate completion card                       |
| Lost submit response; old settled local command     | Exact ID/payload reconciliation outside visible history; no duplicate send under a new ID  |
| App/projector/replica restart, missed notification  | Replay resumes from durable committed checkpoint; segments and coverage agree              |
| Crash between projection writes/checkpoint          | Transaction rollback or complete batch, never partial progress                             |
| Long gap, expired update cache, slow client         | Explicit bounded rebootstrap preserving draft/reading anchor/local IDs                     |
| Late page/lookup/detail, eviction, stream race      | No regression/future contamination; replay buffered suffix or retry explicitly             |
| Oversized single-Event update or raw entry          | Bounded staging/chunks or explicit resync/error; no partial cursor advancement             |
| Old-epoch payload returns after rebuild             | Immutable cache identity cannot collide; stale response cannot replace current bytes       |
| Repeated upward paging during live changes          | No gaps/duplicates; stable viewport, explicit exhaustion, bounded cache                    |
| Raw entry outside window; filtered empty range      | Exact original provenance/order; independent cursor and explicit availability              |
| Sandbox suspended/deleted; runner unreachable       | Archived page readable, controls show evidence/staleness, submit does not pretend to queue |
| Source conflict, unknown observation, corrupt batch | Explicit integrity/projection error; never silently reset or drop evidence                 |
| Auth expiry, cross-user cache, CSRF, stream cancel  | No auth bypass/leak; controlled login/reconnect; no unintended command cancellation        |

Reducer tests compare `snapshot(H) + changes(H,K]` to direct projection at `K`,
including randomized batch boundaries and all command outcome types. Database tests
exercise transactions, indexed bounded reads and cross-replica wakeup races. Frontend
tests exercise subscriber and render boundaries, the full hydration algorithm, and
visual states. Browser acceptance measures transfer, decoded bytes, mounted segments,
first usable view and reconnect work against the measured staging shape and synthetic
long history. Report browser measurements separately from host HTTP/store benchmarks.

## Implementation boundaries

Implement in independently reviewable units: transport/codegen probe; pure projection
and parity tests; transactional read model/rebuild/update journal; RPC reads/follow
and auth; frontend store/Thread cutover; history/payload/Raw demand reads and browser
acceptance. History and payload semantics must be supported by the initial schema,
even if their UI lands later. Delete obsolete full-history stores/reducers and direct
Thread HTTP callers at their replacement cutover; do not retain a compatibility mode.

No runner protocol migration, new command queue, combined Sandbox+Thread creation,
resume implementation, optional retention deletion or conversation-density redesign
is authorized by this document. Other app pages may adopt the chosen request/store
patterns later, preserving their own authoritative versions and domain APIs.
