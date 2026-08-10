# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

Two chat surfaces live here, deliberately. They are separate experiments over one piece of
session machinery, not a migration in progress: Matrix is not replacing the SPA view, and
whether either survives is open (<../../plans/matrix_chat_runtime.md> § Open questions).

## `claude_chat.py` — sessions, sandboxes, and the turn loop

The shared substrate: session and message rows, Postgres `LISTEN`/`NOTIFY`, the SandboxClaim,
the runner WebSocket bridge, and the `handle_runner` turn loop. Also the SPA chat surface's
own HTTP routes and SSE stream, which is the older of the two experiments.

Both surfaces run on it at once. They are ordinary separate sessions — separate rows,
separate sandboxes — so a browser conversation and the Matrix conversation coexist rather
than contend. **Gotcha:** that also means two live sandboxes, and only the Matrix one
announces itself, so the browser one is the easy one to forget you are paying for.

Streaming (`StreamEvent`, and the `asyncio.wait` abort dance around it) exists for the SPA
alone. The Matrix path forwards only a finished turn (R11.1), so if the SPA view is ever
retired that machinery goes with it.

## Matrix chat surface

- `matrix_client.py` — the client-API calls the loop makes, over `matrix-nio`.
- `matrix_sync.py` — logs in as `@haku`, long-polls `/sync`, binds the one room Haku
  services, and hands what the operator types to the session behind it. Holds the only
  Matrix credential, so everything that speaks into the room speaks through it.
- `matrix_session.py` — the room/session binding, ingress (`MatrixTurns`), egress
  (`MatrixReplySink`), and the supervisor that keeps a live session behind the room.

Two behaviours worth knowing before reading the code:

- **A refused batch is not queued here.** `enqueue_prompt` only accepts on a ready session
  with nothing pending, so when it refuses, the sync watermark is simply not advanced and
  the homeserver re-delivers next pass. Queue-until-turn-end (R2.2) and "nothing is silently
  dropped" (R1.6) come out of that, with no second durable queue.
- **One replica syncs.** The loop holds a Postgres advisory lock (`MXSY`) for its lifetime —
  `/sync` is a long poll, so releasing between passes would let two replicas double-process a
  batch. The supervisor is a sibling task holding a **second** lock (`MXSE`), so provisioning
  is single too, while a stalled claim cannot wedge ingress (R1.4). Two locks, not one: they
  are elected independently and can land on different replicas.

## Tests run against a real database

Every store here is exercised through Postgres (the `migrated_*` testcontainer fixtures),
never a stand-in. What stays faked is what is genuinely outside: Kubernetes, the Matrix
homeserver, and the Agent SDK client. The rule is not tidiness — a fake store answers from
the shape the test author imagined, so it agrees with whatever the code does:

- A fake `_listen` passed against a fake engine while the real one raised on **every** call
  in production, because it was written against psycopg3's API on an asyncpg engine.
- A fake conversation store let a test bind a room to a session id that had never existed.
  Postgres refuses: `matrix_conversation.session_id` is a foreign key, so that state is
  unreachable and the test was describing a scenario the schema forbids.

The one deliberate exception is `FailingEngine`, which exists to make a connection fail —
there is no way to ask a healthy Postgres for that.

## `chat_notifications.py` — the wake channel

`LISTEN`/`NOTIFY` for the chat surfaces, deliberately **not** part of `ClaudeChatStore`: a
repository answers questions about rows, and this wakes tasks. Merging the two is what let
the listener be written against psycopg3's API while running on an asyncpg engine.

**One channel, `claude_chat`, carrying a `ChatEvent`** — `{kind, session_id}`, where `kind`
is `prompt`, `update`, or `abort`. It used to be three channels each carrying a bare session
id, which left the event kind implicit in the channel name and every new kind costing
another `LISTEN`. Waiters register on `(kind, session_id)`, so the fan-out is unchanged.
`console_events.py` stays a separate channel and a separate connection: it is a different
subsystem with a different payload and its own lifecycle, and the only thing the two share
is the mechanism.

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
the roll with the old replica still serving. The channel merge was done exactly that way.

The trap in the overlap phase, worth knowing before staging the next one: while both names
are being notified, every wake is delivered twice, so a woken waiter proves nothing about
which name woke it. Tests and production alike will look healthy with the new path entirely
broken, right up until the old one is deleted. Cover the new path end to end on its own
before contracting.

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
- `ClaudeChatSession`, `ClaudeChatMessage`, `MatrixSyncState`, and `MatrixConversation` in
  <../database_schema.py>, plus their Alembic revisions — migrations are one lineage for the
  whole database.
- The `ReplySink` port is defined next to the service that calls it (`claude_chat.py`), and
  the composition in <../app.py> is what ties a sink to a surface.
