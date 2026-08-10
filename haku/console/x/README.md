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

## Cross-replica state, and the trap it sets

`replicas: 2` means any given HTTP request reaches an arbitrary pod, while a session's live
objects — the runner's bridge websocket, its `ClaudeSDKClient`, its abort event — belong to
exactly one. **Anything that has to reach a running turn therefore goes through Postgres
`NOTIFY`, never an in-process registry**; a dict keyed by session id looks correct in tests
and single-replica dev, and silently answers "no such session" in production about half the
time. That is what `_ABORT_CHANNEL` is for, and it is the same mistake the supervisor's
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
