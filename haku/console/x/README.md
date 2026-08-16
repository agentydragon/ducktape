# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

Two chat surfaces live here, deliberately. They are separate experiments over one piece of
session machinery, not a migration in progress: Matrix is not replacing the SPA view, and
whether either survives is open (<../../plans/matrix_chat_runtime.md> § Open questions).

## The directory says which axis a module varies on

Three things vary independently here, and until they were separated the tree could not tell you
which was which. _Matrix is just one of pluggable backends — channels_ (operator, 2026-08-16), and
the harness directory is `claude_code/` rather than `claude/` because the CLI harness and the model
behind it are different axes (operator, 2026-08-15):

| Where                | What it is                                                                                   |
| -------------------- | -------------------------------------------------------------------------------------------- |
| `x/*.py`             | **The runtime.** Sessions, turns, frames, notifications, sandboxes — no channel, no harness. |
| `x/channels/<name>/` | **One channel.** Matrix today; the SPA is served by the runtime's own routes.                |
| `x/claude_code/`     | **One CLI harness.** Named for the product whose binary it launches, not for the model.      |

The rule for placing a module is what it would take with it if the other axis were replaced: a
second channel must be able to reuse everything at the runtime level unchanged, and a module that
cannot compile without `matrix-nio` belongs under `channels/matrix/`. `room_status.py` is the case
worth knowing: it reads as Matrix and is not — a status line is a channel affordance the console
surface wants too, and the driver is handed two coroutines and never learns which room it speaks
to. `sandbox_claims.py` is the mirror case: `claude-`-prefixed claim names, but it is Kubernetes
provisioning and would serve any harness. `session_frames.py` is the one that is genuinely both and
is deliberately left whole: four of its constants are the CLI's own top-level `type` values, while
`SETUP_OUTPUT_KIND` and `setup_output_frame` are the bridge's envelope and the console's own
authored row — the TODO at the top of that file is the same observation, and splitting it is stage 2
of <../../plans/chat_runtime_projection.md> rather than a placement question.

## `session_store.py` and `session_runtime.py` — the rows, and the turn loop over them

The shared substrate: session, message and turn rows, Postgres `LISTEN`/`NOTIFY`, the
SandboxClaim, the runner WebSocket bridge, and the `handle_runner` turn loop. Also the SPA chat
surface's own HTTP routes and SSE stream, which is the older of the two experiments.

**The line between the two files is the transaction.** `SessionStore` owns the SQLAlchemy sessions,
so a method whose job is "these writes commit together or not at all" is in `session_store.py` — the
outbox write and the turn-state transitions included, because each of those commits beside the
effect it describes and moving one out would be the drop it exists to prevent. What is left in
`session_runtime.py` is what drives one turn against a CLI: the client wiring, the room and status
plumbing, the sandbox lifecycle, the `RoomSurface` port and the SPA's own routes. A second channel
inherits the store unchanged, which is why it stays at the runtime level and imports nothing under
`channels/`.

**The tables are `sessions` and `session_*`.** They were `claude_chat_*` — six backend-neutral
concepts named after the one CLI that fills them, while the design requires a second backend to be
representable. Migrations `0040` and `0041` are the expand and contract of that rename, and this
module's own name was the last of it: it is the session runtime, not Claude's.

**A turn is a row, and it is a range over the frame log.** `next_prompt` dequeues a prompt and
opens `session_turns` in one transaction; `_run_turn` is that turn's span and closes it on
every exit, keeping what the `result` frame reported about cost, usage and duration. At most one
turn per session is open (a partial unique index), and that is the single question behind three
answers: whether a prompt may be admitted, whether there is anything to abort, and whether the
SPA is shown `responding` — which the session view now derives rather than reading off a column.
So an open turn on a session nothing is renewing is not a leak; it is the record of an exchange
whose replica went away before anything could close it.

**And the row carries the turn's state, not only its extent.** `assistant_message_id` is the
message being streamed into, `said_anything` whether one has completed, `queued_reply` whether the
room's outbox holds a reply from this turn — each written in the same transaction as the effect it
describes, so none of them can claim something that did not commit. `_run_turn` reads them at the
top of every turn (`turn_state`), which is why adopting a half-finished exchange is a read rather
than a reconstruction: `adopt_open_turn` says _which_ turn, and the row says how far it got. Before
this the state lived in that call's locals and a second body of code rebuilt it from the frames,
which is one state machine written twice
(<../../plans/chat_runtime_projection.md> § stage 3). **Gotcha:** `queued_reply` is the outbox row
existing, deliberately not `sent_at` — an unsent row still means the room is owed that text, so a
turn that re-queued it would post the answer twice — and `said_anything` is a separate column
rather than the same one read twice, because a session with no room queues nothing.

**The rollout is recorded by `RolloutRecorder`, a `FrameSink` the protocol client calls.** Every
frame either way, both channels, verbatim — the control channel included, since an interrupt that
did not take is diagnosable from nothing else. **Deltas included** — a log with a hole in it cannot
be folded over (<../../plans/chat_runtime_projection.md> § stage 1), so "do not bury the reader" is
answered at the read instead: `read_frames` leaves `stream_event` out of its default view. Beside
them the store keeps a single rewritten `partial` row for the answer in flight, which is what makes
an interrupted turn's half-answer survive.

Both surfaces run on it at once. They are ordinary separate sessions — separate rows,
separate sandboxes — so a browser conversation and the Matrix conversation coexist rather
than contend. **Gotcha:** that also means two live sandboxes, and only the Matrix one
announces itself, so the browser one is the easy one to forget you are paying for.

Delta streaming (`StreamEvent`) exists for the SPA alone. The Matrix path forwards whole
assistant messages, each as it completes (R11.1), so if the SPA view is ever retired that
machinery goes with it.

### What has been lifted out of it

Three leaves, each a module nothing in `session_runtime.py` reaches into — so the store, the service and
the turn loop kept their shape while the file lost the parts that never needed to be beside them
(<../../plans/chat_runtime_cleanup.md> § Anytime). `session_store.py` is not one of them: the
service calls it on every path, so that split is a seam and not a leaf.

| Path                | Role                                                                                                                                                                             |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session_frames.py` | The `kind` vocabulary of the frame log, the two frames the console authors itself (`partial` answers and sandbox narration), and the readers that pick a value out of a payload. |
| `session_views.py`  | The read models the API returns for a session or a conversation, and the projection that assembles one out of the session row, its transcript and its rollout.                   |
| `room_status.py`    | The per-turn status driver: what the room is shown while a turn runs, and when. It is handed two coroutines and never learns which room it speaks to.                            |

## The frame inspector — reading the record the transcript is a projection of

`session_messages` is a **lossy projection** of `session_frames`
(<../../plans/chat_runtime_projection.md> § stage 4), and a projection nobody can appeal is a
projection nobody can debug. So a conversation in the console carries a **Raw frames** button, and
`GET /api/conversations/{session_id}/frames` is what it reads: one page of the rollout, in wire
order, each frame's payload whole. Frontend: `frontend/x/session_frames_page.tsx`, at its own route
(`/_console/conversations/<id>/frames`) so a frame is something an operator can link to.

Three decisions worth knowing before changing it:

- **The first page is the tail, and the cursor walks backwards.** `read_operator_frames` is the
  reverse keyset of `read_frames`, which the MCP reader uses forwards. The frames an operator opens
  this for are a session's last ones, and paging forward from frame one to reach them is what would
  make a long session expensive to debug. Each page still comes back in wire order.
- **Deltas are a mode, not a checkbox.** The default view is `read_frames`' own — everything except
  `stream_event` — because a turn streams those in the hundreds and the completed `assistant` frame
  repeats their content. Interleaving them would bury the frames they duplicate, so "Stream deltas"
  asks for that kind alone; the one question they answer is how far an answer got before it was cut
  off.
- **Nothing is clipped, and the page size is what bounds the response.** The MCP reader clips a
  frame to a context budget; here the wire is the answer, so clipping would be the lossy projection
  again one level down. The browser's other cost is drawn down instead: a payload builds its
  syntax-highlighted editor only once it nears the viewport (`frontend/code_block.tsx`).

**Where per-message provenance lands** (`agent/claude-frame-provenance`, #4105): a message's
inclusive `frame_seq` range is a bound on the same query — `before_seq` is already its upper half —
so linking one message to its frames is a filter over this view, not a second read path. It is
deliberately not guessed at here: the join key the transcript has today (`agent_message_id`) is
missing on thousands of production rows, which is why that change exists.

## The neutral projection — what reads it

`conversation_events.py` is the provider-neutral vocabulary a conversation is read as — text,
messages, reasoning, a tool-call lifecycle, harness activity, a completed turn — and
`claude_code/projection.py` is the pure function from Claude's frames into it. Together they are the
one interpreter that <../../plans/chat_runtime_projection.md> § stage 4 replaces four with.

**The two halves sit on different levels, which is the placement rule doing its job.** The
vocabulary names no backend and every surface renders it, so it stays at the runtime level; the
adapter cannot be written without knowing what `assistant`, `stream_event` and `tool_use_result`
are, so it lives under the harness directory. A second backend adds a sibling adapter and touches
neither the vocabulary nor its readers.

**Two readers are on them.** `haku_conversations.read_transcript` reads a stored session:
`SessionStore.read_transcript` folds it and `transcript_entries.py` maps the result onto the MCP
surface's wire models. And now the live path reads them too.

**`_run_turn` is the first of the four onto it**: it projects each frame as it lands and acts on the
events, so the live path no longer knows that `assistant`, `stream_event` and `result` exist.
`adopt_open_turn`, `rollout_calls` and `coarse_status` still read frames their own way; each is its
own change. Nothing stores the events yet either — `session_messages` and the outbox keep the shapes
they had, and the durable cursor beside the fold is the other half of stage 4.

Two consequences of that being live:

- **`DeltaSource` is why a live consumer and a stored log see different `TextDelta`s.** A log cannot
  be read from `stream_event` frames (they carry no identity, are not deduplicated, and a truncated
  one re-projects to different text), while a consumer holding the wire wants exactly those, because
  taking the increments as they arrive is what streaming an answer is. The prose is the same; only
  the cut differs.
- **The turn loop projects one frame at a time, freshly.** A projector held across the turn would
  merge the frames sharing one `message.id` into a single row and defer every completion to the
  frame after it — both improvements, both changes to what is stored, and so neither is here.

Three properties to preserve:

- **`project` is pure and deterministic.** That is what makes drift detectable (re-project a stored
  session and compare), a projection bug repairable (fix the fold and re-project), and it is why the
  function mints nothing random — a message's identity is the `frame_seq` it opened at.
- **Every event carries provenance, and it is a union.** A frame-derived event has a `FrameRange` an
  operator can click through to raw JSON; a console-authored one (narration, an ownership change)
  has no frames at all. A rebuild that treated authored events as re-derivable would delete them.
- **The default branch is counted, not dropped.** `Projection.unprojected` tallies frame classes
  this release has no meaning for, because three frame classes and five `system` subtypes in
  production are already undocumented and the CLI keeps adding them.

Every rule in the adapter is a measured fact from <../debug/frame_shape_census.md> rather than a
reading of `protocol.md` — a message is a whole run of frames that a tool result can interrupt, the
renderable `content` of a tool result is not its result, and every "did this go wrong" field is
uninformative. Read the census before changing what looks like belt and braces.

## Matrix chat surface — `channels/matrix/`

- `client.py` — the client-API calls the loop makes, over `matrix-nio`.
- `sync.py` — logs in as `@haku`, long-polls `/sync`, binds the one room Haku
  services, and hands what the operator types to the session behind it. Holds the only
  Matrix credential, so everything that speaks into the room speaks through it.
- `session.py` — the room/session binding, ingress (`MatrixTurns`), the surface a
  room-backed turn reports through (`MatrixSurface`), and the supervisor that keeps a live session
  behind the room.
- `pacer.py` — one paced outbound queue per room, over Synapse's `rc_message` budget.
- `outbox.py` — the room's outbox: replies as `session_outbox` rows, and the drain that
  says them.
- `formatted_body.py` — Haku's Markdown into the HTML subset Matrix clients render.

**The outbox is half here and half at the runtime level, deliberately.** The row is written where
the reply is produced, by `session_store.update_assistant` into the neutral `session_outbox` table,
in the same transaction as the assistant message; what lives here is the claim-and-send half, which
cannot exist without a homeserver. That split is the shape every outbound channel write has to
take — recorded first, sent from the record — so a second channel inherits the record and writes
only its own drain.

Three behaviours worth knowing before reading the code:

- **A produced reply is a row, not a call.** A turn writes it in the same transaction as the
  assistant message it copies, and `RoomOutboxDrain` — one replica, under the `MXOB` advisory
  lock — claims the oldest, queues it into the pacer, and marks it `sent_at` only once
  `room_send` has returned. Before this, `deliver` was a `deque.append` that returned before any
  request existed, so a refused send was discarded with a log line and a queue holding an answer
  died with its replica (<../debug/message_drops.md>). Everything else the console says — the
  status line, lifecycle and holding notices, bootstrap narration — stays on the pacer's
  in-process queue, because a notice describing a moment is not worth redelivering ten minutes
  later. Two rules the drain is deliberate about: a failed reply **halts** the queue for its
  backoff rather than being overtaken, and the one row stepped over is one out of
  `MAX_SEND_ATTEMPTS`, kept unsent with its `last_error` and logged loudly rather than deleted.

- **A refused batch is not queued here.** `enqueue_prompt` only accepts on a ready session with
  no turn open and nothing pending, so when it refuses, the sync watermark is simply not advanced
  and the homeserver re-delivers next pass. Queue-until-turn-end (R2.2) and "nothing is silently
  dropped" (R1.6) come out of that, with no second durable queue. **Admission is that one
  transaction's alone**: `MatrixTurns.offer` asks no status question of its own, because an answer
  read outside `enqueue_prompt`'s `SELECT … FOR UPDATE` could only agree with a decision that had
  not been made yet. It takes the refusal — `RuntimeError` for a session that cannot take the
  batch, `KeyError` for a session row that has gone — and tells the room it is holding.
- **An accepted batch is not acknowledged either, until its turn ends** (R2.5). Acceptance is a
  prompt row, and a session that ends before claiming it leaves that row where its replacement
  cannot see it, so acknowledging at the enqueue lost the message outright
  (<../debug/message_drops.md> I3). The deferral is a `matrix_held_batch` row: the `/sync` token
  the batch ended at, plus the transcript row it became, which is the durable link on to the turn.
  A turn that **ended** publishes the token — failed and aborted turns included, because holding
  out for an answer wedges ingress behind the first turn that never produces one — and a prompt
  whose session ended without it drops the row and leaves the watermark, so the same messages are
  offered to the replacement session. **The loop reads ahead of what it promises**: while the row
  exists the poll starts from the held token, so a batch already with a session is not re-delivered
  every pass (and `/sync` can still block, which it cannot when asked for data it has already
  sent). There is still no local queue and no message-level dedupe — the homeserver holds the
  batch, and the delivered events are simply behind the cursor.
- **An event Haku cannot read is announced, not held.** `m.text` and `m.emote` are prose and are
  serviced; an `m.image`, `m.file`, voice memo, or an msgtype invented after this release is
  carried out of the sync as an `UnmappableEvent`, said out loud in the room, and then
  acknowledged. Refusing the batch instead would never converge — nothing about an already-sent
  screenshot changes, so it would wedge ingress against every later message — which is the one
  case the paragraph above cannot cover. `m.notice` is neither serviced nor announced: it is the
  msgtype Haku's own status, lifecycle and unreadable-event lines go out under, and excluding it
  from any sender is the second of two independent guards (the first is the R1.5 sender rule)
  against a notice about an event that is itself an event.
- **One replica syncs.** The loop holds a Postgres advisory lock (`MXSY`) for its lifetime —
  `/sync` is a long poll, so releasing between passes would let two replicas double-process a
  batch. The supervisor is a sibling task holding a **second** lock (`MXSE`), so provisioning
  is single too, while a stalled claim cannot wedge ingress (R1.4). Two locks, not one: they
  are elected independently and can land on different replicas.

## Tests run against a real database

Every store here is exercised through Postgres (the `migrated_*` testcontainer fixtures),
never a stand-in. What stays faked is what is genuinely outside: Kubernetes and the Agent SDK
client — the homeserver used to be on that list and is not any more (below). The rule is not
tidiness — a fake store answers from the shape the test author imagined, so it agrees with
whatever the code does:

- A fake `_listen` passed against a fake engine while the real one raised on **every** call
  in production, because it was written against psycopg3's API on an asyncpg engine.
- A fake conversation store let a test bind a room to a session id that had never existed.
  Postgres refuses: `matrix_conversation.session_id` is a foreign key, so that state is
  unreachable and the test was describing a scenario the schema forbids.

The one deliberate exception is `FailingEngine`, which exists to make a connection fail —
there is no way to ask a healthy Postgres for that.

### The runtime's conftest names no channel

The placement rule above applies to the fixtures too, and it is the one place it is easy to lose:
`conftest.py` is inherited downwards, so a runtime-level fixture that reaches into
`channels/matrix/` makes every runtime test depend on a homeserver's vocabulary and makes a second
channel unaddable without dragging Matrix along. So `x/conftest.py` holds the stores, the service,
the claim stand-in and the operator's identity — and nothing a room knows — while the homeserver
identities (`MATRIX_*`), the config they compose into, and the room/session binding
(`conversations`) live in `channels/matrix/conftest.py`. The dependency runs channel → runtime and
never back: `OPERATOR_SUBJECT` is imported down rather than restated, because
`MATRIX_CONFIG.operator_subject` and the `operator_id` fixture have to name the same operator.

The test files divide on the same seam. `test_session_runtime.py` and `test_session_store.py` reach
a room only through the `RoomSurface` port and the `room_id` string a session records — never
`matrix-nio` — so what they cover (the turn loop, the outbox row, provenance, adoption, the abort
drain, prompt fate) is what a second channel inherits. A message _arriving_ from a room and becoming
a turn is the crossing itself and cannot be written without both halves, so `MatrixTurns.offer` is
tested in `channels/matrix/test_session.py`, beside the module that defines it.

### The stand-ins live in `testing/`

Everything a test stands something up _with_ is a module in `testing/` — the claim stand-in
(`testing/recording_claims.py`), the stub `claude` (`claude_code/testing/stub_claude.py`), the
Synapse container (`channels/matrix/testing/synapse_container.py`) and the console replica
(`channels/matrix/testing/console_replica.py`) — rather than a
`conftest.py` fixture or a source file in a target's `data`. Each sits under the axis it stands in
for, so a stand-in cannot outlive the thing it fakes. Two of them are processes a test
starts, and a `py_binary` can import neither a conftest nor a file staged only as data. The
`test_*.py` files stay beside what they test.

What a test drives them _through_ lives there too, so a second e2e inherits it rather than
copying it: `channels/matrix/testing/operator_room.py` is the operator's side of a room — an
`nio.AsyncClient` of its own, handing back typed `RoomEvent`s rather than Matrix JSON —
`channels/matrix/testing/console_deployment.py` is the console replicas serving that room, and
`testing/waiting.py` is the bounded poll under both, at the runtime level because a bounded poll is
nobody's channel. It shares nio with `channels/matrix/client.py` and deliberately **nothing else**: nio is third party and is not the subject,
but `EventTag`, `SyncResult` and the event mapping on top of it are exactly what these tests are
checking, so reading the room back through them would check them against themselves.

The stub `claude` is a `py_binary` for one further reason: the runner execs `HAKU_CLAUDE_PATH`
directly, and a source file staged in runfiles does not reliably carry its executable bit — which
is why both tests used to copy it out and `chmod` it. One stub serves both, parameterised by the
directions a prompt carries (`[hold]`, `[narrate=N]`, `[silent]`) and by `HAKU_STUB_GREETING`.

### The homeserver is a real Synapse as well

`channels/matrix/test_homeserver_e2e.py` brings one up in a container
(`channels/matrix/testing/synapse_container.py`) and runs
`MatrixClient` against it, with the room's other side driven straight through the client-server
API rather than through the client under test. It exists for the questions a canned response can
only agree with: whether a resumed `/sync` across a gap larger than `TIMELINE_LIMIT` really comes
back truncated and really paginates back to every missed message once (R1.7 — the reason the
target exists), whether a sync watermark really is a `/messages` token, whether a repeated
transaction id really is refused, and whether the `works.allegedly.haku` tag survives the wire on
both halves of an edit.

`channels/matrix/test_client.py` keeps its canned homeserver, and should. The split is which side of the
wire the question is about: what Synapse does belongs in the e2e target, what the parser does with
what Synapse said belongs in the unit tests — where an unknown tag kind or an exhausted backfill
budget costs one dict rather than thousands of messages.

### And the whole surface, as processes

`channels/matrix/test_fullstack_e2e.py` composes those two targets with the bridge one: that
Synapse, a console replica as its own process (`channels/matrix/testing/console_replica.py`, with
the real sync loop and supervisor), a runner process per sandbox behind a stub `claude`
(`claude_code/testing/stub_claude.py`),
and a real Postgres. It asserts one operator-facing property — every message the operator sent has
exactly one reply in the final room — which is what nothing below the whole stack can answer.

**Three of its four tests were written failing**, because that property did not hold (R11.6, "a
produced reply must never be lost silently"): a delivery that raised was logged and dropped while
`spoke` was set anyway, the pacer was an in-process queue that died with its replica, and a
console adopting a session skipped the replayed frame as one already recorded.
`channels/matrix/outbox.py` is what closes all three. The fourth is quiet-path and passed throughout, which is what made the
other three attributable to the drop rather than to the harness.

The console is a process, and the sandboxes are started by the test off the claim files that
console writes, for one reason: a sandbox has to outlive the console for there to be an adoption
at all.

## `session_notifications.py` — the wake channel

`LISTEN`/`NOTIFY` for the chat surfaces, deliberately **not** part of `SessionStore`: a
repository answers questions about rows, and this wakes tasks. Merging the two is what let
the listener be written against psycopg3's API while running on an asyncpg engine.

**One channel, `session_events`, carrying a `SessionEvent`** — `{kind, session_id}`, where `kind`
is `prompt`, `update`, or `abort`. It used to be three channels each carrying a bare session
id, which left the event kind implicit in the channel name and every new kind costing
another `LISTEN`. Waiters register on `(kind, session_id)`, so the fan-out is unchanged.
`console_events.py` stays a separate channel and a separate connection: it is a different
subsystem with a different payload and its own lifecycle, and the only thing the two share
is the mechanism.

**Waiters name a session; watchers cannot.** `wait`/`subscribe` register on `(kind, session_id)`,
which is what the turn loop and the supervisor want. `watch` is the other shape — every event of a
kind, whatever session it names — and exists for `session_live_updates.py`, which has to hear about
sessions nothing has told it to expect. Its gotcha is the mirror of the waiters' one: a reconnect
can be replayed to a waiter (`_wake_everyone` tells it to re-read the session it already knows
about) and cannot be replayed to a watcher, so a watcher must be something a missed event only
delays.

`test_notify_puts_a_readable_event_on_the_channel` is the one that pins the wire format —
channel name and envelope, read off a raw connection. Nothing else would notice if either
drifted, because every other test has the same code on both ends.

One long-lived connection with a reconnect loop, matching <../console_events.py>, the
console's other LISTEN consumer. The notify half stays inside the caller's transaction,
because `pg_notify` delivers on commit.

**Gotcha for anyone changing the channel name or payload:** the Deployment rolls with
`maxUnavailable: 0`, so old and new replicas run together for the length of a roll. A
renamed channel means the new replica notifies where the old one is not listening, and the
wakes are lost for that window — the same expand/contract discipline a destructive
migration needs. Notify on both names for one release, then drop the old — and gate that
second release on the roll having **converged** (every pod on an image at or after the
first), not on a release having elapsed, since `maxUnavailable: 0` means a bad image stalls
the roll with the old replica still serving. The channel merge was done exactly that way, and so
was `claude_chat` → `session_events`.

The trap in the overlap phase, worth knowing before staging the next one: while both names
are being notified, every wake is delivered twice, so a woken waiter proves nothing about
which name woke it. Tests and production alike will look healthy with the new path entirely
broken, right up until the old one is deleted. Cover the new path end to end on its own
before contracting — for the session rename that meant a test driving `pg_notify` on exactly
one channel, since `notify` was firing both and so could not answer the question.

## `session_live_updates.py` — telling open tabs, over the socket they already hold

The console's own live channel (<../console_events.py>) reaches every tab the operator has open,
and this is what puts session changes on it: a `SessionChangedEvent` naming the session whose rows
moved. **An invalidation, not a payload** — the transcript stays a REST read, so a tab that missed
events lands correct by refetching and no consumer has to decide whether the socket or the API is
the truth.

Three things it is deliberate about:

- **Nothing publishes anything new.** Every write that changes a session already notifies `UPDATE`
  inside its own transaction, so the announcement belongs to the commit rather than to a sweep.
- **No second `NOTIFY`.** `LISTEN` is broadcast, so each replica already hears every session
  event and turns what it hears into sends on the console sockets **it** holds
  (`ConsoleEventHub.deliver_locally`). Relaying through `broadcast` would notify twice for one
  change and deliver it to every tab twice.
- **Coalescing is the point, not tidiness.** `UPDATE` fires per stream delta, and each event costs
  every open tab a whole transcript — far more than the notification that triggered it. One event
  per session per `COALESCE_WINDOW` (500ms) is what keeps the invalidation cheaper than the refetch.

Routing costs a lookup: `SessionEvent` carries no operator and the hub delivers per operator, so
the session's owner is resolved once per session and kept (a session's owner never changes).

The SPA chat page's SSE stream is untouched and stays until a coalesced refetch is proven in
practice (<../plans/session_channels.md> § 2) — a worse-feeling result should cost a revert of the
surfaces, not of the transport.

## Cross-replica state, and the trap it sets

`replicas: 2` means any given HTTP request reaches an arbitrary pod, while a session's live
objects — the runner's bridge websocket, its `ClaudeSDKClient`, its abort event — belong to
exactly one. **Anything that has to reach a running turn therefore goes through Postgres
`NOTIFY`, never an in-process registry**; a dict keyed by session id looks correct in tests
and single-replica dev, and silently answers "no such session" in production about half the
time. That is what the `abort` event is for, and it is the same mistake the supervisor's
missing lock was.

## What necessarily lives outside this directory

The stable modules own these, so moving them here is not possible without inverting the
dependency:

- `MatrixConfig` and `Settings.matrix` in <../config.py>. Absent config, or a config whose
  reflected bot password has not landed yet, means the surface does not start and the
  console does (R10.3b).
- `Session`, `SessionMessage`, `MatrixSyncState`, and `MatrixConversation` in
  <../database_schema.py>, plus their Alembic revisions — migrations are one lineage for the
  whole database.
- The `RoomSurface` port is defined next to the service that calls it (`session_runtime.py`), and
  the composition in <../app.py> is what ties a surface to a session.

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../../plans/chat_runtime_cleanup.md> and <../../plans/chat_runtime_projection.md> — what is
  still wrong and the order to fix it.
- `debug/` holds dated findings from one incident and is not maintained.
