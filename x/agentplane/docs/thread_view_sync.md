# Thread view synchronization

Status: **proposed contract, not deployed.** This document owns the derived
app-to-browser read API. [Thread layering](thread_layering.md) owns identities,
runner durability, command semantics, and the distinction between conversation,
pending commands, and operational state. The schemas below select API shapes;
they are not checked-in executable protobuf definitions yet.

## Requirements

What the protocol must do, independent of how. Each is falsifiable, and the
[acceptance matrix](#failure-and-acceptance-matrix) says how each is observed. The
decisions below are answers to these; where a decision stops serving one, the decision
is what changes.

1. **Opening is bounded.** What opening a Thread transfers and processes does not grow
   with the Thread's length. A month-old Thread opens like a new one.
2. **A screenful is enough to work.** What arrives first is the part of the conversation
   the reader is looking at, the current controls, and the pending queue — enough to read
   it and to submit a command. Older history and exact evidence load on demand, and never
   load in the background merely because a tab stayed open.
3. **Values stream as they grow.** An assistant message, a reasoning step, and a tool
   call's arguments and output appear while they are being produced, not only once they
   complete. Following a growing value costs what was added to it, not its size on every
   update — otherwise the view costs more than the raw Events it replaces.
4. **Following never reloads.** A reader holding a view receives what changed and never
   re-reads the Thread to stay current. After a gap it resumes from the position it
   holds, or is told explicitly that it must bootstrap again.
5. **A command's fate is observable.** A command can be submitted and what became of it —
   admitted, taken effect, failed, or not yet observed — read afterwards, including across
   a lost response or a reload, without downloading history to find it.
6. **Every step is consistent.** A bootstrap plus every update that follows equals what a
   rebuild at the same position produces. No update leaves a reader holding a state the
   log never passed through.
7. **Nothing is lost underneath.** Everything the view omits stays exactly retrievable.
   The derived view is a convenience over the archive, never a replacement for it.

## Decisions

- Keep the runner's sole command queue and the app's lossless copy of its Events.
  Materialize a rebuildable conversation read model in PostgreSQL. Opening a page
  must not replay the entire Thread on either server or browser.
- Normal mode reads recent assembled segments, current controls, and pending-command
  summaries, then follows compact changes. Raw Events and large payloads are demand
  reads. Neither initial loading nor idle background work downloads full history.
- Carry it over REST and SSE with Pydantic models, keeping protobuf for the messages
  the runner journal already defines. No client or bidirectional stream is needed:
  commands are independent unary calls; subscriptions resume with an explicit cursor.
  A dropped connection does not cancel admitted work.
- Use one original runner cursor space. A projection checkpoint is a position in
  that space, not a new app Event number. Derived changes are explicitly not Events.
- Evaluate TanStack DB as the read-only normalized frontend store. The actual
  library spike supports same-collection atomicity; transport and React integration
  still need their own tests. Do not add a second mutable server-state cache.

The [measured short Thread](../debug/thread_load_20260915.md) had eight turns but
3,091 Events and approximately 1.79 MB of SSE. Completed text/reasoning contained
6,278 bytes. This motivates materialization, not just compression or virtualization.

## Transport: REST and SSE, with protobuf payloads

**Protobuf defines the shapes; REST and SSE carry them.** These are separate decisions and
this document had them fused. The derived types -- `Segment`, `Changes`, `ViewSnapshot`,
`Controls` and the rest of § Positions, segments, and payloads -- are defined in `.proto`
alongside the runner journal's own messages, generated to `_pb2` for Python and
Protobuf-ES for TypeScript, and carried as **proto-JSON** over ordinary FastAPI routes and
SSE. No service definitions, no RPC stubs, no Connect: protobuf here is the schema
language, not the transport.

One schema is the point. TypeScript and Python read the same field names, the same
`oneof` alternatives and the same enum values because they are generated from one file,
so the two ends cannot drift. Defining these types a second time as Pydantic models is
what that buys out of: `Event`'s observation union alone has nineteen variants, and a
second representation of one concept is what <../../../STYLE.md> § General forbids.
`frontend/client.ts` already reads `EventEntry` through Protobuf-ES `fromJson` on a plain
REST response, so this is the existing path applied to more of the surface.

`uint64` cursors are the concrete payoff. Proto-JSON encodes 64-bit integers as
**strings**, which `fromJson` reads back into `bigint`, so a cursor above `2^53`
round-trips exactly with nothing to remember. A hand-declared JSON model does not get
that: a JSON number silently loses precision in the browser above `2^53`, and nothing
reports it.

Pydantic keeps what protobuf is not for: request validation, query and path parameters,
and envelopes that are not domain types. FastAPI needs it there regardless.

**Two costs, so nobody rediscovers them as bugs.** First, a protobuf message is not a
Pydantic model, so it cannot be a FastAPI `response_model` and its shape does not reach
OpenAPI -- those payloads are opaque in `schema.d.ts`, and the browser's types for them
come from `ts_proto_library` instead. That is a real loss for the `responses=` pattern
`live.py` uses to type its SSE frames, and it is accepted here because generated types
from one schema beat generated types from two. An SSE route whose frames are protobuf
declares its frame union as a proto `oneof`; one whose frames are not still declares a
Pydantic model in `responses=`. What is not acceptable either way is today's
`/threads/{id}/events/stream`, whose frame names live in a hand-rolled parser in
`client.py` and a matching `switch` in TypeScript with nothing checking them against each
other.

Second, protobuf `oneof` in Python is `WhichOneof(...)` string comparison, which
<../../../STYLE.md> § General discourages in favour of `isinstance` narrowing. Dispatch
stays close to the boundary rather than spreading string compares through the app.

### Backed out: protobuf RPC over Connect

An earlier revision of this document chose Connect -- `@connectrpc/connect-web` against
a `connecpy` ASGI mount -- and it was built as far as a served `ThreadEvents.FollowEvents`
with the browser following it. It is backed out, not because it failed, but because the
generated typed stubs did not pay for what surrounded them:

- **Its main benefit was already available.** Typed protobuf in the browser comes from
  `ts_proto_library` on the `.proto`, not from the transport: `client.ts` gets
  `EventEntry` through `fromJson` over plain REST today. Connect's remaining benefit over
  proto-JSON on SSE is a generated _frame union_ and binary framing -- and the frame union
  is obtainable from a declared Pydantic union, while binary framing mattered most for the
  raw-event firehose this design exists to stop sending.
- **No Python client follows an open stream.** connecpy's generated async client yielded
  zero frames in 20 s from a stream whose first frame is immediate. `acceptance/agent.py`
  consumes this stream, so the RPC could not replace the SSE without hand-decoding
  envelopes in the one place a library was supposed to help.
- **The surrounding work was the cost.** An ASGI mount inherits no FastAPI route
  dependency, so authorization needed re-plumbing to reach it; each generated service
  needed a `mypy.ini` `warn_unused_ignores` exemption; and the binary/JSON default differs
  between the two ends.
- Protobuf `oneof` in Python is `WhichOneof(...)` string comparison, which
  <../../../STYLE.md> § General discourages in favour of `isinstance` narrowing. The
  derived types are ours to define, so they get the better form.

What is backed out is the **RPC layer**, not protobuf: the schemas, the generated types on
both ends and proto-JSON on the wire all stay, and the derived types join them. The Bazel
rule in <../../../devinfra/python/connect.bzl> and `app/transport_probe/` remain; removing
them is separable and not required by this.

Reconsidering an RPC transport is deferred rather than closed, tracked as
`THREAD_VIEW_TRANSPORT` in <../plans/task_dag.md>. Its gate is authorization: the browser
credential and what an RPC surface would need from it are settled in
<operator_federation.md> § Why the browser holds a handle and not a token, and an RPC
transport should be revisited only against that, not on transport ergonomics alone. The
message definitions are the durable part; a `service` block on top of them is the cheap
part to add later.

### Rejected: gRPC-Web through an Envoy translation hop

Rejected before Connect was, and still rejected: the transport that replaced Connect is
in-process too, so both constraints below bind at least as hard now.

gRPC-Web — `grpc.aio` plus a standard translator — was built and measured, and lost.
It is not infeasible: a spike put a
browser-shaped gRPC-Web request through Envoy into a `grpc.aio` servicer running the
same fold, and `envoy.filters.http.grpc_web` is in `cilium/proxy`'s build, so the
gateway could carry it. Two constraints killed it anyway.

**It costs the test that covers the production wire.** `//x/agentplane/app:test_thread_browser`
drives real Chromium against the real app over the _exact_ wire production uses,
because the RPC is in-process. Put Envoy in front in production and that stops being
true: the test either grows an Envoy container — in a test already running PostgreSQL,
the app and a browser — or it exercises a path production does not have. The
translation hop is precisely the part that cannot be reasoned about from the source,
and it is the part the test would stop covering.

**It does not buy a Python client for the browser's wire**, which was the argument for
moving. `grpcio` is mature, but it speaks _native gRPC_ straight to the server and skips
the translation hop, so a test using it covers the servicer and nothing about what the
browser reads. Covering that wire means decoding gRPC-Web's 5-byte envelope by hand —
the same envelope, for the same reason, as Connect's. Neither transport has a Python
client that follows a browser's stream. (This is moot for our own tests, which drive the
service's fold and leave framing to the library, but it was the reason to switch.)

Smaller, and not decisive on their own: a second listener needs its own `Service` port,
`NetworkPolicy` and readiness, where the Connect mount is an `app.mount()` on the ASGI
app that already exists; the 71 lines of Envoy config must agree with the proto's service
name, the bound port and the browser's path with nothing checking that; and each gRPC
service costs a `mypy.ini` `warn_unused_ignores` exemption for `mypy-protobuf`'s
generated stub.

**Authorization does not distinguish them**, and no part of this rejection rests on it:
bearer-token-in-metadata versus this app's session cookie is a header decision either
transport carries identically, and `fetch` sends the cookie for Connect today. What the
browser credential is, and what a bearer token would cost, is decided in
<operator_federation.md> § Why the browser holds a handle and not a token.

What is genuinely transport-coupled is thinner than "swap the adapter" suggests: the fold
raises `ConnecpyException`, Connect's vocabulary, so a second adapter either translates a
Connect exception type or the fold mints a third error type both adapters translate.

Spike code and its measurements: commit `7e1a36d8` (branch
`claude/exciting-turing-83lyjb-grpcweb-spike`, never merged, so this record rather than
the code is the durable part).

Generation must use standard Bazel rules and pinned local plugins: keep `@protobuf`
message targets and Aspect `ts_proto_library` for the protobuf payloads, and the existing
OpenAPI-to-TypeScript path for everything Pydantic. No second TypeScript generator, no
shell-driven codegen, no checked-in stubs.

Transport acceptance is what the browser test already covers, extended: incremental
server frames before EOF, cancellation, terminal errors mid-stream, cookies, auth expiry,
and cursors above `2^53`. Then exercise the deployed ingress -- buffering, idle timeout,
reconnect, replica replacement. Extend `//x/agentplane/app:test_thread_browser`, which
runs a real browser, app and PostgreSQL, and retain app draining behaviour for open
streams.

Every route uses the existing caller/session authorization boundary, including resource
access checks. Require same-origin requests, explicit cookie/CSRF protection for
mutations, no wildcard credentialed CORS, and structured unauthenticated errors instead of
HTML login redirects inside a stream. Bound stream lifetime and recheck authorization so
an expired or revoked login cannot read forever. Cancellation releases
listeners/transactions; it never issues an interrupt command.

## Schema ownership and service surface

Keep `protocol/{command,event,event_log}.proto` harness-neutral and unchanged by view
requirements. Add `app/thread_view.proto` for the derived segment/change types, which
reuses the generated `Command`, `EventEntry`, `EventOrigin`, item/turn enums and
timestamps by importing them rather than redeclaring them. The runner must not import the
app file. These are message definitions only -- **no `service` blocks**, since nothing
generates RPC stubs from them. There is no product identity named Conversation.

Three route groups under `/threads/{thread_id}`, named for what they own rather than by
a service suffix:

```text
view      GET  /view                 one consistent ViewSnapshot
          GET  /view/follow          SSE: ViewUpdate = Changes | RebootstrapRequired
          GET  /segments             bounded SegmentsPage around an anchor
          GET  /payloads/{ref}       bounded bytes; HTTP range, not an envelope
commands  POST /commands             the archived runner CommandAdmitted EventEntry
          GET  /commands/pending     CommandsPage of pending summaries
          GET  /commands             CommandLookup for bounded command IDs
events    GET  /events               EventsPage of exact entries
          GET  /events/by-origin     EventsByOrigin for bounded EventOrigin references
          GET  /events/stream        SSE: the unfiltered original Event log
```

The commands group covers submission and queries of both pending and settled commands.
Its reads share the view's materialized checkpoint; the route boundary does not introduce
an app-owned queue or another ordering. The table below names each operation by its row
in this list.

Operational inventory and runner status remain a separate authority. Existing live
inventory endpoints stay. A later replacement can offer unary reads and server-streaming
snapshots with its own version and staleness metadata; it cannot borrow a Thread
projection cursor. Sandbox CRUD, egress and action policies, Actions, connections, consent
and settings are not converted by this work.

The shapes are in `thread_view.proto`. What follows is what a schema cannot state about
itself.

**One integer timeline.** The runner journal's dense sequence. The app archives each Event
under that same number rather than assigning its own, which `TrajectoryStore.record`
enforces, so a Segment's position, a Command's admission and a payload's revision are
directly comparable. A projection checkpoint is a position in that space, never a new app
Event number, and derived changes are explicitly not Events.

All limits have server-enforced maxima. Page tokens are opaque, bound to Thread,
source/epoch, direction and filter; not authorization capabilities. Counts are
useful because responses can be partial. Empty pages distinguish exhaustion from a
scan/byte limit and an unavailable source. Paged reads represent values larger than
their byte budget by references. For oversized raw entries, chunk the serialized
`EventEntry` and decode it with the same generated schema after reassembly.
The Event stream remains exact-entry: if an entry exceeds its frame limit, terminate the
stream with a typed error frame identifying the original entry and its chunk reference. Resume after that entry only once retrieved and verified; never
skip its cursor or turn the reference into a fabricated Event. Similarly, an
oversized admission response reports the archived receipt reference; it is not a
rejection of the already admitted command. Validate this boundary in transport
tests, and choose normal unary limits to fit accepted Command sizes.

- `source_id` is the original runner journal identity. A changed source is an integrity and
  recovery condition, not a routine cache reset. Where the app has not observed that
  identity yet, answer with an explicit projection-not-ready error rather than inventing a
  source for an empty view. A known empty source can legitimately sit at a zero checkpoint.
- `projection_epoch` identifies one coherent materialization generation. It is not an Event
  counter and not a compatibility version. A rebuild publishes a new epoch atomically, and
  clients rebootstrap rather than combine generations.
- `through_cursor` covers every original Event through that position, including Events that
  produced no Segment. Only a successful atomic installation advances it. A Segment's
  revision says when that Segment last changed, not what a browser has consumed.

**Most Events are carried, not transcribed.** Only an Item is folded, because only an Item
accumulates across Events -- it is why a derived view exists at all. An Event whose meaning
is already its final state is carried verbatim, so a reader learns one vocabulary rather
than a parallel restatement of it, and keeps the Event's timestamp and causal
`source_sequences`. Three things are genuinely derived, each joining or accumulating rather
than renaming: `Item`, `CommandSummary`, and `Controls`.

Command Events drive the queue and the controls, and produce no Segment. Grouping runs of
tool calls and reasoning is a rendering rule over whatever a client is showing, not a fact
about the log, so nothing marks one.

`Controls` carry the evidenced applied model, active-turn identity and observed harness
state. Initial configuration is separately identified as configuration provenance.
`Attached` may be ahead of the archive and must not seed these Event-derived values.

**A growing value streams as its growth.** A batch extending a Segment the caller is known
to hold carries only what was added; re-sending a value whole on every update costs the sum
of its prefixes, which is worse than the raw deltas the view replaces. A Segment the
interval created arrives whole, as does a value replaced rather than grown -- authoritative
arguments are not an extension of the partial JSON they supersede. A caller that cannot
apply an extension re-reads that Segment rather than inventing a base. Snapshots and history
pages only ever carry whole values.

**Payloads are immutable at a reference**, scoped to the source and projection epoch, so a
corrected rebuild cannot reuse an old derived payload's cache identity. Exact raw references
remain epoch-independent original Event identities, not derived payload references, and
`ReadPayload` with an `EventOrigin` returns serialized `EventEntry` bytes. A payload is
returned whole: a finished value is never split across responses. UTF-8 byte offsets within
one are not JavaScript string indices.

Unloaded, loading, loaded-empty, streaming, failed-fetch and unavailable payloads are
distinct UI states. Loaded payload caches are immutable and separate from mutable Segment
metadata; they never become another authority for item completion.

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
coverage. Coalescing within a batch must retain every new historical Segment and
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
    F->>A: GetView {thread:T, window:{max_older:50}}
    A-->>F: Snapshot {source:S, epoch:E, through:900, window, pending, controls}
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

A batch that extends a Segment the caller holds carries only what was added, so an Item
streaming over hundreds of Events costs its final size rather than the sum of its prefixes.
A caller that cannot apply an extension re-reads that Segment.

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
The count must not imply the visible first page is the whole queue. Compare generated
Commands on reconciliation, not just summary text or IDs.

On a short reconnect, follow from the last installed position. Batches must start at
that position; duplicates require matching identity/content. Partial overlap or a
gap is not guessed through. Recover by rereading/rebootstrap; report conflicting
content as an integrity error. Connection loss leaves visible data marked stale.

On a long gap or epoch change, cancel the old subscription, increment the frontend
request generation, and get a new bounded snapshot whose window is where the reader is
parked rather than the tail. Install its segments, controls, commands and position
atomically, then follow, and reconcile any locally retained Command ids with
`GetCommands` against the position just installed. Preserve drafts, disclosures and
viewport position, not stale server truth. Old generation callbacks cannot write into the
new store. An unavailable cursor is reported with a reason; the client does not silently
jump to the tail.

```mermaid
sequenceDiagram
    participant F as Browser offline at 970
    participant A as App through 500000
    F->>A: FollowView {T, S, E, after:970}
    A-->>F: RebootstrapRequired {UpdateCacheExpired}
    F->>F: Cancel old generation. Retain draft and viewport at cursor 400
    F->>A: GetView {T, window:{from_cursor:400, max_older:25, max_newer:25}}
    A-->>F: Snapshot {through:500000, window around 400, controls, pending}
    F->>A: GetCommands {T, ids:[C], minimum_through:500000}
    A-->>F: Lookup {C settled}
    F->>F: Atomic replacement without missed-token replay or jump to bottom
    F->>A: FollowView {T, S, E, after:500000}
    A-->>F: Changes {500000..500020}
```

## History and live-data races

History is keyset-paginated by each Segment's immutable cursor, not by timestamp or
offset. A new Segment cannot appear behind a cursor a caller already processed, and an
updated old Item keeps the cursor it started at.

**Chosen consistency:** each page is a current short DB snapshot at `P`, at least
the request's `minimum_through`. Adjacent pages need not share a long-lived historic
DB snapshot; stable cursors prevent insert-induced holes. A page's Segment revisions
are at most `P`. It does not advance the browser's live cursor.

The view stream includes compact changes for every affected segment, including those
outside loaded windows. The browser keeps a **bounded** recent change buffer, not all
those segments. This permits race-free hydration without a bidirectional subscription
or per-browser server-side window registry:

1. At installed cursor `H`, request the page with `minimum_through:H`; retain change
   batches from `H` while the request is outstanding.
2. A response at `P > current_cursor` waits until the live stream reaches `P`.
   It must not inject future state into an earlier snapshot.
3. If the browser is already at `K >= P`, merge the page plus buffered changes `(P, K]`
   for those Segments in one transaction. Ignoring a late lower-revision Segment is
   sufficient only where a newer whole Segment is already loaded; an evicted Segment may
   need the buffered extensions replayed onto the page, which is why the buffer exists.
4. If the needed buffer was evicted, an extension names a revision the page does not
   carry, or the epoch or source changed, discard and retry hydration or rebootstrap
   bounded windows. Never declare a stale page current. Requests and buffered bytes have
   explicit limits.

A page always carries whole Segments, so it is a valid base to replay extensions onto. An
extension for an unloaded Segment is buffered for a pending hydration rather than applied
to a fabricated empty Item. A page becomes observable only after that replay; showing an
unreconciled page as current would violate the checkpoint contract. Where a minimum cursor
cannot be served before the request deadline, report projection lag rather than return an
older successful page.

```mermaid
sequenceDiagram
    participant F as Browser at 1000
    participant A as App
    F->>A: ListSegments {before_cursor:400, minimum_through:1000, max_segments:50}
    A->>A: Read segments and checkpoint P=1010 consistently
    A-->>F: Changes {1000..1010}
    A-->>F: Changes {1010..1030, update old item I}
    A-->>F: Delayed SegmentsPage {covers 350..400, whole Segments incl. I}
    F->>F: Merge page + buffered 1010..1030 for its segments
    Note over F: Still through 1030. Preserve visible segment and pixel offset
    F->>A: ReadPayload {I.output at revision 1020}
    A-->>F: The whole value at that reference, without changing the live cursor
```

Pending-page and command-lookup hydration obey the same position/generation rules.
For pending membership, live settlement removes an entry even if it raced the page;
new admissions have higher cursors and are found through live updates. An immutable
payload needs no Segment overwrite: cache it by reference and show it only while that
reference is selected, or label a historical revision explicitly.

Keep a bounded tail window and, while reading far back, a bounded window around the
viewport. Evict/refetch intervening history; do not accumulate a month of segments just
because the tab stayed open. Pin controls, pending-page metadata and local commands,
not the entire turn. Prepending preserves the visible segment plus pixel offset. New
output sticks to the bottom only if the user was already there. Virtualization
bounds mounted DOM independently of network/cache bounds.

## Raw and debug surface

Raw remains additive to the same conversation order and disclosures. Show source,
Segment cursor and revision, installed projection cursor, applied command evidence and
separately reported archive/runner positions. Fetch exact frames and causal
`source_sequences` only when requested. Raw chronological entries retain original
cursor order even when an aggregate card spans interleaved streaming Events.

`ListEvents` reports the original range it scanned, which bounds what an empty page
proves and is where a caller resumes from. A direct evidence lookup can find Native frames
before the visible page and command Events, which the conversation carries no Segment for.
The raw follower is an explicit debugging option, not a background
requirement for normal mode. Neither raw pagination nor direct lookup moves the
conversation checkpoint. A live Raw panel follows archive availability separately
from the potentially lagging projection.

Availability is typed: retained, not-yet-archived, and unavailable. Transport failure is
not empty evidence. Raw authentication is no weaker than conversation authentication, and
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

| Case                                                | Required observation                                                                                                              |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Short high-delta or month-long Thread               | Bounded first payload and work; assembled past text; no eventual full-history fetch                                               |
| First screenful only                                | The visible conversation, controls and pending queue render and accept a command before any history or evidence is fetched        |
| Item streaming over hundreds of Events              | Text and tool output appear while produced; bytes followed are proportional to what was added, not to the value's size per update |
| Empty/new Thread; projector behind archive          | Valid empty snapshot or explicit projection-not-ready; never fake successful catch-up                                             |
| Native-only interval                                | Coverage advances without fabricated conversation segments                                                                        |
| Active pre-window item, huge turn/output            | Current controls intact; bounded segments/previews; exact demand-loaded content                                                   |
| Model queued during Codex turn                      | Admission visible as pending; picker changes only on evidenced effect                                                             |
| Interrupt or command failure                        | Original target/cause and error shown; no fabricated interrupted/completed state                                                  |
| Claude-coalesced input                              | Exact confirmed text and all origin command IDs, at native confirmation position                                                  |
| Failed turn without output                          | Terminal Event visible with its error, without Raw and without a duplicate completion card                                        |
| Lost submit response; old settled local command     | Exact ID/payload reconciliation outside visible history; no duplicate send under a new ID                                         |
| App/projector/replica restart, missed notification  | Replay resumes from durable committed checkpoint; segments and coverage agree                                                     |
| Crash between projection writes/checkpoint          | Transaction rollback or complete batch, never partial progress                                                                    |
| Long gap, expired update cache, slow client         | Explicit bounded rebootstrap preserving draft and viewport; local ids reconciled on the new position                              |
| Late page/lookup/detail, eviction, stream race      | No regression or future contamination; buffered extensions replayed onto the page, or an explicit retry                           |
| Single Event revising many Segments                 | The whole batch or none of it; no partial cursor advancement                                                                      |
| Old-epoch payload returns after rebuild             | Immutable cache identity cannot collide; stale response cannot replace current bytes                                              |
| Repeated upward paging during live changes          | No gaps/duplicates; stable viewport, explicit exhaustion, bounded cache                                                           |
| Raw entry outside the loaded window                 | Exact original provenance and order; independent cursor and explicit availability                                                 |
| Sandbox suspended/deleted; runner unreachable       | Archived page readable, controls show evidence/staleness, submit does not pretend to queue                                        |
| Source conflict, unknown observation, corrupt batch | Explicit integrity/projection error; never silently reset or drop evidence                                                        |
| Auth expiry, cross-user cache, CSRF, stream cancel  | No auth bypass/leak; controlled login/reconnect; no unintended command cancellation                                               |

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
