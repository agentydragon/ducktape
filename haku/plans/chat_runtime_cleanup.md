# Chat runtime — what is wrong, and the order to fix it

Three sources feed this: a design review of `haku/console/x/` and its schema, a second review that
read the code de novo once the first's findings had landed, and a live investigation into sessions
that record nothing but a container boot and a death
(<../console/debug/2026_08_13_sessions_boot_and_die.md>). Items are deleted from here as they land,
so everything below is work that has not been done.

Two ambitions shape the ordering, both stated by the owner and neither of them a demand to
generalize now: the runtime should keep a sandbox across a console roll, and it is
single-Claude-CLI, single-Matrix-room by construction where both are eventually wanted plural. They
matter here because several pieces of obvious tidying are the _wrong_ tidying if done without them
in view, which is said at each such point.

## How the stages are ordered

Two constraints still bind, and everything else is preference:

1. **The frontend seam before the backend seam.** The first is smaller, is pure refactoring, and
   does not touch the schema until its last step; the second changes where meaning is extracted.
2. **Contract halves land when their roll converges**, independently of all of this.

The lifecycle work came first for a reason worth keeping in view: a session had to survive a console
roll before the TTL that was recycling wedged sessions could be relaxed, and had to survive being
asked for before it could be allocated on demand. The roll itself is the only thing that exercises a
turn inherited across it, so that path is proven in production rather than only by tests
(<../console/debug/2026_08_13_sessions_boot_and_die.md>).

## Stage 6 — allocate a sandbox because there is something to do

An idle room holds a sandbox permanently: the supervisor provisions whenever the room has no live
session, and the warm pool is `replicas: 0` so every claim is a cold start. This used to read
"twelve cold starts a day for a room nobody speaks in", each announcing itself and narrating its
bootstrap there — the deadline slide that made a session always-up
(<matrix_chat_runtime.md> R3.2a) removed the recycling, so what is left is
the other half of the same waste: **one sandbox held indefinitely** for a room nobody speaks in,
~1 CPU / 2Gi of an 8 CPU / 16Gi quota with nothing using it. Fewer cold starts, the same standing
cost, and the argument for allocating on demand is unchanged.

The SPA has a gesture that means "I want a session" and Matrix has none, so the supervisor
substitutes by assuming demand permanently. The prompt is the honest substitute, and the prompt
queue already made it cheap: accepted and running were separated when `session_prompts` landed.

- `create()` writes the row and stops, in a new **`idle`** status; `allocate()` mints the credential
  and the claim and moves to `provisioning`.
- Admission accepts on `idle`, so `enqueue_prompt` is what creates demand; the supervisor's trigger
  becomes "an unclaimed prompt and no sandbox", waking on `SessionEventKind.PROMPT`.
- `MatrixTurns.offer` stops refusing an unallocated session, so the batch enters the durable queue
  rather than being left on the homeserver.
- **`LIVE_SESSION_STATUSES` currently means both "worth keeping" and "has a lease to renew".** An
  idle session is the first that is worth keeping with no holder to lose; split the set rather than
  giving it a fake far-future lease.
- **Adding an enum value is two releases here**, not additive: `TextBackedStrEnumColumn` parses the
  column, so a replica on the previous image reading `idle` fails rather than degrading. Widen the
  member and the CHECK first; write it next release.

**Cost, stated plainly:** the first message after quiet pays the full cold start. Measure it rather
than assume it away.

**Done when** an idle room holds no sandbox and the first message provisions one.

## Stage 7 — the two seams

_Everything above is possible without these; they are what stop the next feature from being a
rewrite._

### The frontend seam — first, and mostly refactoring

`RoomSurface` takes `room_id` on all six methods and `MatrixSurface` opens five of them with
`del room_id`, because its channel is already bound to the one room. So the port reads as multi-room
and behaves as one-room-globally, and `room_id: str | None` threads through the turn loop making four
call sites re-ask a question answered once per connection — plus three no-op coroutines so
`_TurnStatus` has something to call.

1. A `ChatFrontend` port with **no address parameter**: `MatrixSurface` bound at construction as it
   effectively already is. Every `or room_id is None` and all three no-ops go.

   **The SPA's implementation is no longer a null one.** This step used to say the SPA needs none
   of the port because its client reads the message rows — true of a view, false of a channel, and
   the console is being driven toward the latter (<../console/plans/session_channels.md>): it wants
   the lifecycle and narration the room gets today, pushed rather than polled. The port is
   unchanged and still right; what changes is that both sides implement it. That plan's
   `chat_attachment` need is this step's schema half, not a separate table.

2. Then the schema half — `chat_attachment(session_id, surface, address, attached_at, detached_at)`
   with a partial unique index on `(surface, address) where detached_at is null`. It subsumes
   `sessions.surface`, `.room_id`, both check constraints tying them together, and
   `matrix_conversation.session_id`; the pointer/history distinction those two tables document in
   prose becomes `detached_at IS NULL`. Attach/detach within one session becomes a row.

### The backend seam

Launch and transport are already a package boundary, the transcript's shape is already neutral, and
the frontends are a different question. What is welded is **meaning**: `_run_turn` matches on the
CLI's own frame `type` and six helpers parse Anthropic content blocks, so the turn loop is a frame
interpreter. Every branch of it reduces to five events naming nothing provider-specific —
`TextDelta`, `MessageCompleted(id, text, calls)`, `ToolResult`, `Activity`,
`TurnCompleted(outcome, text, cost, usage, duration)` — with one adapter per backend producing them.
`ClaudeCli.frames()` already sits where that adapter goes; it yields wire dicts, which is why the
console became the interpreter.

Two corrections this forces on items that would otherwise look like cleanups:

- **The stored calls must end in a `tool_call` table**, written by the adapter — not in a parse of
  Claude frames, which only sessions on that backend have. It answers the lossy-copy objection by
  holding the result, and it is where the rollout join lands anyway. Migration 0047 did the
  vocabulary half early (`session_messages.tool_calls` stores `{call_id, tool_name, arguments}`);
  what is left is the table, which is the half that moves data.
- **"Projection or primary" is settled against the projection.** The rollout is one protocol's wire
  and cannot be the record a second backend shares. Transcript primary, rollout per-backend evidence
  — so what to remove from the duplication is the double write and `agent_message_id`.

**Done when** a second frontend and a second backend are representable without touching the turn
loop's signatures.

## Anytime — independent of the stages

- **Every stream delta re-reads the whole session.** `update_assistant` NOTIFYs per delta →
  `_sse_stream` wakes → `store.get` → `_rollout_calls` selects every `assistant`/`user` frame and
  re-parses its content blocks. O(session) per token batch, paid only while the SPA is streaming.
  Fix by indexing incrementally on the agent message id, or by scoping the read to the messages
  asked about. **Still open, and now paid on a second path**: live session updates landed (#4132)
  as invalidate-then-refetch, so an open tab reads the whole conversation again on every
  invalidation. Coalescing (500 ms per session) is what makes that affordable rather than what
  makes it cheap — the operator's eventual direction is for the backend to stream the increment
  instead, recorded in <../console/plans/session_channels.md> § 4.
- **Split `session_runtime.py`** further. `KubernetesSandboxClaims`, the view models and their
  projection, the frame vocabulary, the status driver and now the store have left; what remains is
  the recorder, the service, the turn loop, the port and the routes. The store went out whole —
  `session_store.py`, drawn on the transaction, since a method whose writes must commit together is
  the store's whatever it is about — rather than along the seams the turn table and the prompt
  queue created, which would have split `SessionStore` itself and put transactions in two files.
  What is left to cut is inside the service: `handle_runner`'s admission and finalisation are a
  connection's lifecycle and `_run_turn` is a frame reducer, and those are two files' worth of
  concern in one class. Each split lands with the change that creates it, never as a standalone
  reshuffle with no acceptance criterion.
- Smaller: "surface" names five things and "turn" three.

## When their rolls converge

- **The prompt queue's compatibility half.** The transcript row is still minted `pending` and the
  `_legacy_pending` scan still answers a prompt an old replica accepted. Both tombstoned. Write the
  row final and drop the scan; `'pending'` stays in the CHECK constraint.
- **`tool_uses`.** Server default and "stop writing it" both landed with migration 0047, which
  added `tool_calls` beside it. What is left is the `drop_column`, once no replica running the
  previous image can still select the mapped column by name.

## Later

- **Mid-turn steering.** Measured to work: a prompt written while a turn runs is absorbed at the next
  tool boundary and one `result` covers both. `session_turn_prompts` is many-to-one already and
  admission deliberately still refuses, with a test saying so. Needs `interrupt` with
  `cancel_queued: true`, since a bare interrupt starts the next queued prompt.
- **Streaming the answer into the room.** Achievable only as a coarse refresh — the five-second floor
  is Synapse's rate limit and what a person can read, and every edit is a permanent federated event.
  Stream into the status line's mechanism (a notice, edited, redacted at the end) rather than editing
  the reply, because editing the reply puts partial drafts into the tail `recent_messages` reads and
  the msgtype trick that excludes chatter does not apply to an `m.text` edit.
- **Source the CLI from npm** rather than out of the Agent SDK wheel
  (<cli_protocol_ownership.md>).
