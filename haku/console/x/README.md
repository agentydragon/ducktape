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

**The tables are `sessions` and `session_*`, and the rename is mid-flight.** They were
`claude_chat_*` — six backend-neutral concepts named after the one CLI that fills them, while the
design requires a second backend to be representable. Migration `0040` renamed them and left each
old name behind as an auto-updatable view, so a replica on the previous image keeps reading and
writing the same rows for the length of a roll; `../test_session_table_compatibility.py` is what
proves the views really carry the previous release's writes, `ON CONFLICT` inference included.
**The contract migration that drops those views has not landed**, so nothing may assume the old
names are gone yet.

**A turn is a row, and it is a range over the frame log.** `next_prompt` dequeues a prompt and
opens `session_turns` in one transaction; `_run_turn` is that turn's span and closes it on
every exit, keeping what the `result` frame reported about cost, usage and duration. At most one
turn per session is open (a partial unique index), and that is the single question behind three
answers: whether a prompt may be admitted, whether there is anything to abort, and whether the
SPA is shown `responding` — which the session view now derives rather than reading off a column.
So an open turn on a session nothing is renewing is not a leak; it is the record of an exchange
whose replica went away before anything could close it.

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

## Matrix chat surface

- `matrix_client.py` — the client-API calls the loop makes, over `matrix-nio`.
- `matrix_sync.py` — logs in as `@haku`, long-polls `/sync`, binds the one room Haku
  services, and hands what the operator types to the session behind it. Holds the only
  Matrix credential, so everything that speaks into the room speaks through it.
- `matrix_session.py` — the room/session binding, ingress (`MatrixTurns`), egress
  (`MatrixReplySink`), and the supervisor that keeps a live session behind the room.

Two behaviours worth knowing before reading the code:

- **A refused batch is not queued here.** `enqueue_prompt` only accepts on a ready session with
  no turn open and nothing pending, so when it refuses, the sync watermark is simply not advanced
  and the homeserver re-delivers next pass. Queue-until-turn-end (R2.2) and "nothing is silently
  dropped" (R1.6) come out of that, with no second durable queue. The gate asks
  `session_turns` whether an exchange is in flight; it used to ask whether the session's
  status was `ready`, which happened to mean the same thing only because `enqueue_prompt` itself
  wrote `responding`.
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

### The homeserver is a real Synapse as well

`test_matrix_homeserver_e2e.py` brings one up in a container (`synapse_container.py`) and runs
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

**Right now it is two names, mid-expand.** `claude_chat` is `LEGACY_CHANNEL`, notified and
listened on beside the new one so neither half of a roll goes deaf, and dropped in the contract
release — see the constant for the gate.

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
the roll with the old replica still serving. The channel merge was done exactly that way, and
so is the `claude_chat` → `session_events` rename in flight now.

The trap in the overlap phase: while both names are being notified, every wake is delivered
twice, so a woken waiter proves nothing about which name woke it. Tests and production alike
will look healthy with the new path entirely broken, right up until the old one is deleted.
`test_an_event_on_one_channel_alone_wakes_the_waiter` is what answers it — it drives
`pg_notify` on exactly one name, which `notify` itself can no longer do.

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
- The `ReplySink` port is defined next to the service that calls it (`claude_chat.py`), and
  the composition in <../app.py> is what ties a sink to a surface.

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../../plans/chat_runtime_cleanup.md> and <../../plans/chat_runtime_projection.md> — what is
  still wrong and the order to fix it.
- `debug/` holds dated findings from one incident and is not maintained.
