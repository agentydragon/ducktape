# Chat runtime cleanup

What is left of two design reviews of `haku/console/x/` and the schema it writes. Findings are
deleted from this file as they land, so everything below is work that has not been done. Nothing
here is a bug report in the sense of "production is broken": the runtime works and is in use.

The second review (2026-08-13) read the code de novo, after the first review's findings had landed.
Its theme is different from the first's: the data model is now mostly honest, and what remains is
**one seam drawn in the wrong place** — the messaging surface — plus the cost of the transcript and
the rollout being two records of the same thing.

Ordered by payoff, not by size.

## The room is a parameter that nothing reads

`RoomSurface` (<../console/x/claude_chat.py>) takes `room_id` on all six of its methods.
`MatrixSurface` (<../console/x/matrix_session.py>) opens five of the six with `del room_id`: its
`RoomChannel` is the sync loop, which is already bound to the one room the console services. So the
parameter that makes the port look multi-room is discarded by the only implementation, and the
service holds exactly one `room_surface` for the whole process.

That is the worst of both designs. It reads as "a session names its room when it speaks", which
would be the shape that supports several rooms, several surfaces, or a session that moves — and it
behaves as "there is one room, globally", which is what `app.py` wires. A reader cannot tell which
is true without opening both files.

The cost is not only conceptual. `room_id: str | None` is threaded from `handle_runner` through
`_run_turn` into `_deliver_reply`, `_turn_status` and `_progress_reporter`, and each of those
re-asks the same question — `if self._room_surface is None or room_id is None` — about one fact
resolved once per connection. `_TurnStatus` needs three module-level no-op coroutines
(`_ignore_status`, `_ignore_clear`, `_ignore_typing`) to have something to call when the pair is
absent.

**The shape that fits.** Resolve the surface once, into one object with no `room_id` parameter:

```python
class ChatFrontend(Protocol):        # what a session talks to, whatever that is
    async def system_prompt(self, session_id: UUID) -> str | None: ...
    async def deliver(self, text: str) -> None: ...
    async def report(self, detail: str) -> None: ...
    async def show_status(self, text: str) -> None: ...
    async def clear_status(self) -> None: ...
    async def set_typing(self, active: bool) -> None: ...
```

with a null implementation for the SPA — which needs none of it, because its client reads the
message rows — and `MatrixSurface` bound to its room at construction, as it effectively already is.
Every `or room_id is None` disappears, the three no-op coroutines disappear, and the port stops
claiming an addressing capability it does not have.

**Then attach/detach becomes expressible.** One frontend per session is still a design decision, but
it becomes a decision rather than an accident: a session would hold a _set_ of attachments, resolved
per delivery from a registry keyed by surface kind, and attaching a second one mid-session is a row
plus a set member rather than a change to every signature. That is what makes "the same Claude
session, reachable from the room and from the browser" or "moved to a different room" sayable.

**The schema half of the same seam.** The binding lives in two Matrix-shaped places:

- `claude_chat_sessions.surface` + `.room_id`, with two check constraints tying them together, and
  a `SpaSession | MatrixSession` pair of variants in code whose only content is that column pair;
- `matrix_conversation`, keyed by the bot's MXID, holding the room and a pointer to the live
  session — plus a documented cross-row agreement that SQL cannot state and a test stands in for.

A second surface makes `room_id` the wrong column name, `matrix_conversation` the wrong table, and
that check-constraint pair unmaintainable. One relation subsumes all of it:

```text
chat_attachment(session_id, surface, address, attached_at, detached_at | None)
    -- address: a Matrix room, a channel, NULL for the SPA
    -- partial unique index on (surface, address) where detached_at IS NULL
       => "one live session per room", the property matrix_conversation encodes by its primary key
```

The pointer/history distinction the two tables carry today falls out of `detached_at` instead of
being documented prose: history is every row, the pointer is the undetached one. `surface`,
`room_id`, both check constraints, `matrix_conversation.session_id`, and the `SpaSession |
MatrixSession` variants all collapse into it, and `create()` stops taking a surface at all.

This is the largest item here and the one with the clearest payoff. It is also, unlike the rest,
a migration.

## Every stream delta re-reads the whole session

`update_assistant` NOTIFYs on each delta; `_sse_stream` wakes and calls `store.get`; `store.get`
calls `_rollout_calls`, which selects **every** `assistant` and `user` frame of the session and
parses each payload's content blocks to rebuild two indexes. So one token batch costs a scan and a
JSON parse of the entire conversation so far, including every tool result in it — and then a
`model_dump_json` of a view that embeds the whole transcript.

That is O(session) work per delta, on a session whose frames grow without bound. It only bites while
an SSE stream is open, so Matrix traffic does not pay it today and nobody has noticed; the SPA on a
long session is where it shows up. The console already learned this lesson once, on the
past-tool-calls page (<../console/debug/past_tool_calls_perf.md>).

Two independent fixes, either sufficient:

- index the calls incrementally — the join key is the agent's message id, so a call and its result
  can be resolved when the frames arrive rather than rebuilt on every read;
- or scope the read: the SPA only ever renders messages it is showing, and `_rollout_calls` could
  take the message ids it is being asked about.

## The status line has two owners and loses updates between them

`_TurnStatus` decides what the room should say and remembers what it last said (`_shown`);
`MatrixSyncService.show_status` decides whether the room may be told (`STATUS_EDIT_INTERVAL_SECONDS`,
five seconds) and remembers what it last sent (`_status_body`). Two dedupes and one rate limiter for
one line.

They disagree in a way that loses state. `_TurnStatus` marks a state shown **before** calling the
sink; the sink drops it when it lands inside the edit floor and does not keep it. The driver then
sees `_state == _shown` and never retries. So a status that changes twice within five seconds
leaves the room reading the older of the two until the _next_ change — which, on a turn that then
settles into one long tool call, is the rest of the turn.

The fix is to give pacing one owner: the driver holds the desired state and the sink is an
idempotent "make the line say X", retaining a value it could not send yet and flushing it when the
floor passes. Not "call `_show` more often" — that puts the retry in the layer that was trying not
to think about pacing.

## The transcript and the rollout are two records of one conversation

`claude_chat_messages` is now largely derivable from `claude_chat_frames`: an assistant message _is_
an `assistant` frame, the prompt text _is_ the `user` frame the console sent. What the transcript
adds is state the wire does not carry — `pending`, the incrementally-rewritten `streaming` row, and
`error` — plus the SPA's ordering.

The duplication is not free, and it is visible per delta: `_run_turn` writes the streamed text to
the message row _and_ to the `partial` frame row, two writes of the same string per token batch, and
the transcript's `tool_uses` is the lossy copy of what the frames hold verbatim (below). It is also
where the interesting `NULL`s come from: `agent_message_id` is NULL exactly for the rows the console
synthesized rather than observed.

Worth deciding rather than drifting: is the transcript a **projection** of the rollout — derived,
rebuildable, holding only what the wire does not say — or is it the primary record with the rollout
as an audit log beside it? Today it is written as if it were primary and read as if it were
derived. Choosing "projection" retires `tool_uses`, `agent_message_id` and the double write; choosing
"primary" means saying so and dropping the derivation in `_message_view`.

## `bridge_token_fingerprint = b""` is a boolean wearing a credential's clothes

The empty-bytes value means "claim cleanup done" — `claim_cleanup_candidates` selects
`!= b""`, `complete_claim_cleanup` writes `b""`. So a credential column carries a third state that
is not a credential, and the sentinel is the zero value that STYLE says never to use for absence.

It wants to be its own column (`claim_cleaned_at: datetime | None`, which also records _when_), and
the fingerprint wants to be nullable-or-not on its own terms.

## A frame's kind is typed as a message's role

`_rollout_calls` filters `ClaudeChatFrame.kind.in_([ChatMessageRole.ASSISTANT, ChatMessageRole.USER])`
and then compares `kind == ChatMessageRole.ASSISTANT`. Those are different domains that happen to
share two spellings: a frame's `kind` is the CLI's top-level `type` (`assistant`, `user`, `system`,
`result`, `stream_event`, `control_request`, …), while `ChatMessageRole` is our transcript's
two-valued role. The pun works until either side gains a value, and it reads as a bug on the way
past. `kind` deserves its own `StrEnum` in `chat_models.py` — the same place `FrameDirection`
already lives — or plain string literals.

## "Surface" means five things and "turn" means three

In two files: `ChatSurface` (the column enum), `SessionSurface` (the `SpaSession | MatrixSession`
union), `surface_column` (the field on those variants), `RoomSurface` (the port), `MatrixSurface`
(its implementation). And `MatrixTurns` (ingress), `ClaudeChatTurn` (the row), `_run_turn` (the
loop). Most of this dissolves with the attachment relation above; what does not is worth renaming on
its own — `MatrixTurns` is an ingress adapter, and `_TurnStatus` drives the room's activity
indicators rather than holding a turn's status.

## The comments carry the project's history, not the code's meaning

A large share of the prose in `claude_chat.py`, and some in `matrix_session.py` and
`database_schema.py`, describes what the code used to be and which bug that caused. STYLE puts
"historical 'used to' comments" under **Remove**, and roughly 150 of `claude_chat.py`'s ~2000 lines
are that. A sample, all of which state something true and none of which the reader needs to
understand the present code: the `surface_column` comment describing the `isinstance` chain it
replaced; five lines on admission asking about a turn rather than a status; "No status write" and
"No `chat.status = RESPONDING` here" explaining absent statements; the `TaskGroup` comment
describing "the previous hand-rolled cancel/await/suppress dance"; `RoomSurface`'s four lines on the
three callbacks it replaced; `RoomChannel`'s docstring, two-thirds of which is the four dependencies
it replaced; `ClaudeChatTurn`'s opening paragraph, which is the world before turns existed.

The distinction worth keeping: a comment that says **"do not reintroduce X, because Y"** is an
invariant and earns its place — one line, in the imperative, next to the thing it guards. A comment
that narrates how the code got here belongs in `debug/`, where it can be read by someone who is
looking for it.

## Smaller, mechanical

- **`claude_chat.py` is ~2000 lines** holding view models, the Kubernetes claim client, the store,
  the frame recorder, the status driver, the service, the turn loop, the surface port and the FastAPI
  routes. The seams are now obvious: the ~230 lines of `KubernetesSandboxClaims` plus its five
  dict-poking helpers have nothing to do with chat, and the routes and view models are a third
  cluster. This is the store-splitting item below, arriving from the other direction.
- **`_run_turn` carries five overlapping pieces of turn-local state** — `assistant_id`, `streamed`,
  `saw_assistant_message`, `spoke`, `result` — and its epilogue branches over three of them.
  `spoke` and `saw_assistant_message` differ only for an assistant message that made tool calls and
  said nothing, which is nowhere written down.
- **`claude_chat_turn_prompts` has no reader.** It is written in `next_prompt` and selected by
  nothing. That is deliberate — it is the shape mid-turn folding needs — but it is write-only until
  folding lands, which is worth knowing when reading the schema.
- **`MatrixTurns.offer` pre-checks the session status** before calling `enqueue_prompt`, which
  checks it again atomically and raises. The pre-check's own comment admits it races. It buys a
  nicer log line; catching the refusal buys the same line without the TOCTOU.
- **`list_turns` and `read_frames` take `session_id: str`** and immediately parse it, while every
  other store method takes a `UUID`. The string comes from the MCP tool, and the parse belongs at
  that boundary.
- **The store imports its read models from `tools/conversations.py`** — `Conversation`,
  `TurnRecord`, `RolloutFrame`. The store depending on the MCP tool that reads it is backwards; the
  models want to live beside the store, with the tool importing them.
- **`_message_view`'s `_NO_CALLS` default** serves exactly one caller: `enqueue_prompt`, returning a
  user row, which cannot have tool calls at all.
- **`KubernetesSandboxClaims._clients()`** is double-checked locking with four `assert`s to satisfy
  the type checker. One `_Clients` dataclass built under the lock would need none of them.

## Carried from the first review

### Mid-turn steering works and we are not using it

Measured, not inferred (<../cli_protocol/probes/steering.py>, 2026-08-12): a prompt written to the
CLI while a turn is running is **absorbed at the next tool boundary**, the model acts on it, and one
`result` frame covers both prompts. <matrix_chat_runtime.md> R2.2a defers this as having "no native
mechanism"; that is now corrected there.

Nothing on our side prevents it either — writing a prompt to the CLI is a bare `transport.write()`
with no interlock. What prevents it is the shape of our loop: `_run_turn` drains to the `result`
frame before looking for the next prompt.

So `MatrixTurns.offer` can stop refusing batches during a turn (R2.2 becomes fold-into-turn) and
"actually, skip the calendar part" reaches Haku while it is working. The turn model landed the shape
this needs — `claude_chat_turn_prompts` is many-to-one already — and deliberately did not turn it
on: admission still refuses a second prompt while a turn is open, and a test says so.

A fold is confirmable rather than merely visible in what the model does next: `ClaudeCli.query`
stamps a `uuid` on the prompt, which is what makes the CLI report `command_lifecycle`, and
`completed` before the turn's `result` means folded.

Two cautions. A turn with no tool call has no boundary to absorb at, so the fallback to next-turn
delivery stays. And the events the bundled CLI documents are `@internal`, so this wants the same
version-pinning discipline as the FastMCP adapter.

**The abort path needs `cancel_queued`.** A bare `interrupt` cancels the running turn and the CLI
then **starts the next queued prompt** — measured, <../cli_protocol/probes/steering.py>. Our abort
means "stop, and drop what I asked for next", which is `interrupt` with `cancel_queued: true`; it
reaches only uuid-stamped commands, which ours now are.

### The prompt queue's compatibility half is still in place

`claude_chat_prompts` is the queue — one row per prompt, `claimed_at` for whether it is still
waiting, a partial unique index making "one in flight per session" a property of the schema. What
still runs beside it is the shape it replaced: the transcript row is minted `pending`, and
`ClaudeChatStore` still falls back to scanning the transcript for one, so a prompt an old replica
accepted mid-roll is still answered. Both are tombstoned in the code.

Once the roll converges, write the transcript row final and drop the `_legacy_pending` scan.
`'pending'` stays in `ck_claude_chat_messages_status` — dropping it is a destructive migration for
no benefit.

### `tool_uses` is a column with almost no reader left

`claude_chat_messages.tool_uses` holds id/name/input and no result. The frames beside it hold both,
verbatim, so `ClaudeChatSessionView` takes each call **and** its result from the rollout, joined by
the agent's own `msg_…` id, which the transcript row records. The column is read only for a row with
nothing to point at: one written before that pointer existed, or one the console synthesized rather
than observed (a turn whose text arrived only on the `result` frame).

**Deleting it takes two more releases.** `tool_uses` is `nullable=False` with only a Python-side
`default=list`, so the ORM attribute cannot go until the column has a server default
(`SET DEFAULT '[]'::jsonb`), and the `drop_column` cannot share a release with that — an old
replica's `_message_view` selects the mapped column by name. The synthesized-message case has to
stop needing it first: either those rows get their calls recorded as frames, or they keep having
none, which is what they have today.

### An expired lease should mean unowned, not dead

`lease_expires_at` is a creator-granted provisioning budget before a runner attaches
(`PROVISION_LEASE`, ten minutes) and an owner heartbeat afterwards (`LEASE_TTL`, ninety seconds), and
`lease_holder` now says which of the two is running and which pod holds it. What it still means when
it expires is **dead**: `expire_stale_leases` fails the session and the supervisor provisions a
replacement. <cli_protocol_ownership.md> wants it to mean **adoptable** instead — which cannot land
before an adopter exists, since reinterpreting expiry on its own leaves a room silent behind a
healthy-looking row.

### `ClaudeChatStore` is a god object

Twenty-odd methods across session lifecycle, prompt queue, transcript, frames, turns, leases and
claim-cleanup bookkeeping. It splits along the seams the turn table and the prompt queue created:
sessions/leases, prompts, transcript, rollout. Not as a PR of its own — a standalone reshuffle has no
acceptance criterion and would conflict with everything else here; each split lands with the change
that creates its seam.
