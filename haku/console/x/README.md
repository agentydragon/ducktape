# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

Two chat surfaces live here, deliberately. They are separate experiments over one piece of
session machinery, not a migration in progress: Matrix is not replacing the SPA view, and
whether either survives is open (<../../plans/matrix_chat_runtime.md> § Open questions).

## `claude_chat.py` — sessions, sandboxes, and the turn loop

The shared substrate: session, message and turn rows, Postgres `LISTEN`/`NOTIFY`, the
SandboxClaim, the runner WebSocket bridge, and the `handle_runner` turn loop. Also the SPA chat
surface's own HTTP routes and SSE stream, which is the older of the two experiments.

**The tables are `sessions` and `session_*`.** They were `claude_chat_*` — six backend-neutral
concepts named after the one CLI that fills them, while the design requires a second backend to be
representable. Migrations `0040` and `0041` are the expand and contract of that rename.

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

Three leaves, each a module nothing in `claude_chat.py` reaches into — so the store, the service and
the turn loop kept their shape while the file lost the parts that never needed to be beside them
(<../../plans/chat_runtime_cleanup.md> § Anytime).

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

## Matrix chat surface

- `matrix_client.py` — the client-API calls the loop makes, over `matrix-nio`.
- `matrix_sync.py` — logs in as `@haku`, long-polls `/sync`, binds the one room Haku
  services, and hands what the operator types to the session behind it. Holds the only
  Matrix credential, so everything that speaks into the room speaks through it.
- `matrix_session.py` — the room/session binding, ingress (`MatrixTurns`), the surface a
  room-backed turn reports through (`MatrixSurface`), and the supervisor that keeps a live session
  behind the room.
- `matrix_pacer.py` — one paced outbound queue per room, over Synapse's `rc_message` budget.
- `matrix_outbox.py` — the room's outbox: replies as `session_outbox` rows, and the drain that
  says them.

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

### The stand-ins live in `testing/`

Everything a test stands something up _with_ is a module in `testing/` — the claim stand-in
(`recording_claims.py`), the stub `claude` (`stub_claude.py`), the Synapse container
(`synapse_container.py`) and the console replica (`matrix_console_replica.py`) — rather than a
`conftest.py` fixture or a source file in a target's `data`. Two of them are processes a test
starts, and a `py_binary` can import neither a conftest nor a file staged only as data. The
`test_*.py` files stay beside what they test.

The stub `claude` is a `py_binary` for one further reason: the runner execs `HAKU_CLAUDE_PATH`
directly, and a source file staged in runfiles does not reliably carry its executable bit — which
is why both tests used to copy it out and `chmod` it. One stub serves both, parameterised by the
directions a prompt carries (`[hold]`, `[narrate=N]`, `[silent]`) and by `HAKU_STUB_GREETING`.

### The homeserver is a real Synapse as well

`test_matrix_homeserver_e2e.py` brings one up in a container (`testing/synapse_container.py`) and runs
`MatrixClient` against it, with the room's other side driven straight through the client-server
API rather than through the client under test. It exists for the questions a canned response can
only agree with: whether a resumed `/sync` across a gap larger than `TIMELINE_LIMIT` really comes
back truncated and really paginates back to every missed message once (R1.7 — the reason the
target exists), whether a sync watermark really is a `/messages` token, whether a repeated
transaction id really is refused, and whether the `works.allegedly.haku` tag survives the wire on
both halves of an edit.

`test_matrix_client.py` keeps its canned homeserver, and should. The split is which side of the
wire the question is about: what Synapse does belongs in the e2e target, what the parser does with
what Synapse said belongs in the unit tests — where an unknown tag kind or an exhausted backfill
budget costs one dict rather than thousands of messages.

### And the whole surface, as processes

`test_matrix_fullstack_e2e.py` composes those two targets with the bridge one: that Synapse, a
console replica as its own process (`testing/matrix_console_replica.py`, with the real sync loop
and supervisor), a runner process per sandbox behind a stub `claude` (`testing/stub_claude.py`),
and a real Postgres. It asserts one operator-facing property — every message the operator sent has
exactly one reply in the final room — which is what nothing below the whole stack can answer.

**Three of its four tests were written failing**, because that property did not hold (R11.6, "a
produced reply must never be lost silently"): a delivery that raised was logged and dropped while
`spoke` was set anyway, `matrix_pacer` was an in-process queue that died with its replica, and a
console adopting a session skipped the replayed frame as one already recorded. `matrix_outbox.py`
is what closes all three. The fourth is quiet-path and passed throughout, which is what made the
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
- The `RoomSurface` port is defined next to the service that calls it (`claude_chat.py`), and
  the composition in <../app.py> is what ties a surface to a session.

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../../plans/chat_runtime_cleanup.md> and <../../plans/chat_runtime_projection.md> — what is
  still wrong and the order to fix it.
- `debug/` holds dated findings from one incident and is not maintained.
