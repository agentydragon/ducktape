# Chat runtime cleanup

What is left of two design reviews of `haku/console/x/` and the schema it writes. Findings are
deleted from this file as they land, so everything below is work that has not been done. Nothing
here is a bug report in the sense of "production is broken": the runtime works and is in use.

The second review (2026-08-13) read the code de novo, after the first review's findings had landed.
Its theme is different from the first's: the data model is now mostly honest, and what remains is
**two seams** — the messaging surface, drawn in the wrong place, and the agent backend, not drawn at
all — plus the cost of the transcript and the rollout being two records of the same thing.

The runtime is single-Claude-CLI, single-Matrix-room by construction, and both are wanted plural
eventually. That is not a demand to generalize now; it is what makes some of the tidying below the
wrong tidying if done without it in view, which is said at each such point.

The first item is not a cleanup at all — it is a behaviour change the owner asked for, and it is
first because it is the one with a running cost.

Ordered by payoff, not by size.

## A sandbox should be allocated because there is something to do

**Not the fix for sessions that boot and die** — that is a live defect with its own note,
<../console/debug/2026_08_13_sessions_boot_and_die.md>, and doing this first would move the failure
from "always" to "whenever somebody speaks", which is when it costs a person something. This item is
about cost and noise, and it should follow that one.

Today a Matrix room holds a sandbox permanently, whether or not anyone is talking to it.
`MatrixSessionSupervisor.supervise_once` provisions whenever the room has no live session, so the
steady state is: claim, pod, CLI, idle, `session_ttl_seconds` expires (**7200s**), session fails,
supervisor provisions again. The warm pool is `replicas: 0` — claim-specific env forces a cold start
in Agent Sandbox v0.5.1 — so each cycle is a full cold start.

For a room nobody speaks in, that is **twelve cold starts a day**, each announcing `provisioning a
sandbox · session …` into the room and then narrating its bootstrap there (R7.1), while holding
~1 CPU / 2Gi of an 8 CPU / 16Gi namespace quota continuously. And each replacement already discards
the CLI's context and re-awakens from the last twenty room messages (R3.3a) — so the current design
pays the context loss on a two-hour timer regardless of whether the conversation was going anywhere.

**Why it is like this.** The SPA has a gesture that means "I want a session": the operator clicks,
`POST /api/claude/sessions` runs, and the claim is that click. Matrix has no gesture, so the
supervisor substitutes for one — and the substitute it uses is _assume demand, permanently_. The
honest substitute is the prompt itself.

**What makes it cheap now.** The prompt queue already separated "accepted" from "running":
`claude_chat_prompts` is durable, `next_prompt` claims it whenever the turn loop gets there, and the
turn loop already blocks on a `PROMPT` notification. So a prompt can be accepted against a session
whose sandbox does not exist yet, and the allocation becomes another consumer of the same signal.
Before the queue landed this would have needed a second durable queue; now it does not.

**The split.** A session row is free; a SandboxClaim is not, and `ClaudeChatService.create` does both
in one call. Separate them:

- `create()` writes the row and stops. New status **`idle`**: the session exists, nothing is
  allocated, and nothing is wrong.
- `allocate()` mints the rendezvous credential, creates the claim, and moves to `provisioning` —
  which keeps its current meaning, "a claim exists and we are waiting for the runner".
- Admission accepts on `idle` as well as `ready`, so `enqueue_prompt` is what creates demand.
- The supervisor's trigger becomes "this session has an unclaimed prompt and no sandbox" instead of
  "this room has no live session". It already wakes on notifications; `ChatEventKind.PROMPT` is the
  one to wake on.
- `MatrixTurns.offer` stops refusing an unallocated session. The batch is then taken into the durable
  queue rather than left on the homeserver for the watermark to re-deliver, and `holding N
message(s)` gives way to something truer — the sandbox is starting _because_ of that message.

**Two things this touches that are easy to get wrong.**

`LIVE_SESSION_STATUSES` currently means both "worth keeping" and "has a lease somebody must renew".
An idle session is the first that is worth keeping and has no holder to lose, so `expire_stale_leases`
must stop treating membership in that set as "must have a live lease" — otherwise every idle session
is swept the moment its lease passes, which is the opposite of the intent. Split the set, or exclude
`idle` from the sweep explicitly; do not give an idle session a fake far-future lease.

And **adding an enum value is a two-release change here**, not an additive one. `TextBackedStrEnumColumn`
parses the column into `ChatSessionStatus`, so a replica on the previous image reading an `idle` row
fails rather than degrading. Release one: add the member and widen `ck_claude_chat_sessions_status`.
Release two: start writing it. The same discipline `0033` used for the queue.

**The cost, stated plainly.** The first message after a quiet period pays the full cold start — pod
start plus bootstrap — where today it is answered by an already-warm sandbox. That is the trade: a
permanently-held sandbox buys latency for the first message after silence. It is mitigable
(`_report_holding` already tells the room its message is waiting; the typing indicator already runs;
a warm pool above zero would cover it if the annotation rendezvous the manifest mentions lands) but
it is real, and it is the thing to measure after the change rather than assume away.

**Its natural pair, which is a separate decision.** Allocating on demand only saves the idle case if
something also _releases_ on idle — otherwise the first message of the day still pins a sandbox until
the TTL. An idle timer (no turn for N minutes → drop the claim, back to `idle`) is the obvious pair,
and it costs the CLI's context each time it fires. That cost is already being paid every two hours by
the clock, so an idle timer moves it rather than adding it — and `--resume` (design A in
<cli_protocol_ownership.md>) is what would make it nearly free. Worth doing second, and measured.

**It composes with the backend seam below**: a direct-model backend has no sandbox to allocate at
all, so "the session exists" and "its resources exist" want separating regardless of this.

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

## A second agent backend, and where it would have to plug in

Wanted eventually rather than now: the Codex CLI, or direct model calls with no CLI at all, beside
the Claude Code CLI this was built around. Nothing here needs building yet. It is written down
because the cheap moment to draw this seam is before there are two backends, and because it
**changes what "clean" means for two items below**.

Four layers are Claude-shaped today, and only one of them is genuinely coupled:

1. **Launch and transport** — `claude_bridge`, `ClaudeSession`, `build_claude_launch`, and the
   SandboxClaim's warm pool. Already a package boundary; a second backend is a second implementation
   behind it.
2. **Wire → meaning. This is the coupling.** `_run_turn` matches on `frame.get("type")`
   (`stream_event`, `assistant`, `result`) and six module helpers parse Anthropic content blocks:
   `_content_blocks`, `_text_delta`, `_agent_message_id`, `_coarse_status`, `_rollout_calls`,
   `_assistant_frame`. `end_turn` keys `total_cost_usd`/`usage`/`duration_ms` off the `result` frame.
   The turn loop _is_ a Claude CLI frame interpreter, and each of those readers would need a second
   case.
3. **The conversation record** — `claude_chat_messages`, whose shape is already neutral, beside
   `claude_chat_frames`, which is verbatim wire and therefore necessarily per-backend.
4. **The frontends** — the seam above, and independent of this one: what a session talks to and what
   answers it are different questions.

**The event the loop actually wants.** Every branch of `_run_turn` reduces to one of five things,
none of which is Anthropic-specific:

```python
TextDelta(text)                                  # something is being written
MessageCompleted(agent_message_id, text, calls)  # a message finished, with what it asked for
ToolResult(call_id, content, is_error)           # …and what came back
Activity(description)                            # what to put on the room's status line
TurnCompleted(outcome, text, cost, usage, duration_ms)
```

One adapter per backend produces that stream, the loop dispatches on it, and the six helpers move
inside the Claude adapter. `ClaudeCli.frames()` already sits exactly where that adapter goes — it
yields wire dicts, which is precisely why the console became the interpreter.
<cli_protocol_ownership.md>'s "type only the frames we act on" landed for the control channel and
deferred the conversation channel on the grounds that nothing acted on those frames yet. Something
does: the five branches above.

**The rollout generalizes; its name does not.** `direction` is already named for the agent rather
than for the console and the runner, `kind` is already free text, and `payload` is already opaque
JSONB — so another backend's wire fits the table as it stands. What does not fit is console code
reading `kind` without knowing which backend wrote it: `_rollout_calls` filters `assistant`/`user`,
which are Anthropic's words for those frames. The backend belongs on the session row, and any reader
of `kind` belongs behind the adapter.

**This corrects the `tool_uses` item below.** That item's plan is to delete the column and read tool
calls from the rollout. With one backend that is strictly better, because the frames hold the results
the column never did. With two it makes tool calls visible only for sessions whose backend happens to
speak Anthropic frames — a Codex session would render no tool cards at all. The neutral fix is the
same shape one table further along: a `tool_call(message_id, call_id, name, input, result, is_error)`
row written by the adapter, which is what `tool_uses` should have been. It answers the lossy-copy
objection by holding the result, and it is where the rollout join lands anyway. Delete `tool_uses`
into _that_, not into a frame parse.

**And it settles "projection or primary".** With one backend the transcript looks redundant beside
the rollout. With several, the transcript is the only cross-backend record of the conversation and
the rollout is per-backend evidence beneath it. So: transcript primary, rollout an audit log — the
opposite of where the duplication item below leans, and the reason to decide it deliberately.

**What direct model calls break that a second CLI does not.** The session lifecycle —
`provisioning` → the runner dials in → `bridge_connected_at` → `ready`, with a bridge token and a
SandboxClaim — is the statement "a remote process connects back to us". A direct backend has no such
process: the session is ready when created and the console drives the turn in-process. The lease
still means something (a replica died mid-turn); the rendezvous does not. So the backend also decides
which of those statuses are reachable, which is worth knowing before `ChatSessionStatus` is treated
as universal.

**Naming, cheap and expensive.** `ClaudeChatStore`, `ClaudeChatService`, `ClaudeChatSessionView` are
free to rename when the seam lands. The `claude_chat_*` tables and `/api/claude/sessions` are not,
and renaming them buys nothing a comment does not. Neutral names in code, historical names in
Postgres and in the URL — said once here so it does not get re-litigated later.

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

## A replacement session gets far less history than "the last twenty messages"

`RE_AWAKENING_MESSAGES = 20` is what R3.3a promises a replacement session: enough of the
conversation to pick up a thread mid-topic. What it actually gets is thinner, and thinnest exactly
when it matters most.

`MatrixClient.recent_messages` passes `limit` straight to `/messages` as the **page size**, then
filters the page down to `RoomMessageText`. So twenty is a budget of _timeline events_, not of
messages — and this room's timeline is mostly the console talking to itself. Per session start:
`provisioning a sandbox · session …`, one notice **per chunk of bootstrap output** (the progress
reporter forwards `SetupOutput` as it arrives), and the supervisor's status reports. Per turn: the
status line's creation, its edits, and its redaction. Every one of those is a `m.notice`, which is
excluded from the history read — correctly, it would be noise — but each still consumes one of the
twenty events fetched.

Sessions currently cycle roughly every eighty minutes
(<../console/debug/2026_08_13_sessions_boot_and_die.md>), so that chatter is dense. A re-awakening
in a quiet room can therefore come back with two or three real messages, or none, while believing it
asked for twenty. The failure is silent: the prompt renders with whatever it found.

**The fix already exists three methods away.** `_backfill` loops up to `MAX_BACKFILL_PAGES` at
`TIMELINE_LIMIT` events per page until it reaches what it is looking for. `recent_messages` wants
the same shape: page backwards until it has `limit` **qualifying messages** or a page cap is hit, so
the number means what its name says. Cheap, local, and it makes the promise true.

**What "smarter" could mean beyond fixing the count.** R3.3a chose the room as the source on the
grounds that it is also what the operator sees, so the prompt and the room cannot disagree — which
was right, and was decided when the console had no durable transcript of its own. It has one now:
every prompt it accepted and every assistant message is in `claude_chat_messages`, joinable to the
room through the session's `room_id`, structured into turns, with the tool calls beside it. So the
two sources answer different questions — the room for agreement with what the operator sees, the
store for completeness and structure — and a re-awakening could use both rather than reading a flat
tail of one. Worth deciding before adding cleverness to the tail read; and note that streaming into
the room (below) would put edit events into that same tail, which is a third reason this read needs
to become selective rather than positional.

## Room events should say what they are, instead of being inferred from their msgtype

Every question the console asks about a room event today is answered by a proxy. "Is this
conversational?" is answered by `RoomMessageText` versus `RoomMessageNotice`. "Is this ours?" is
answered by the sender. "Which transcript row is this?" cannot be answered at all. Matrix event
content takes custom keys, so the console can simply say it — a namespaced object on every event it
sends, naming the session, the transcript row, the agent's own `msg_…` where there is one, and what
kind of thing this is (reply, status line, lifecycle notice, bootstrap line, preview).

What that buys, mostly by retiring inferences already documented above as fragile:

- **The history read stops being a msgtype heuristic.** `recent_messages` keeps `m.text` and drops
  `m.notice` because that happens to separate conversation from chatter today. It stops separating
  them the moment anything conversational is not an `m.text` — which streaming into an edited notice
  is exactly — and it can never express "this notice mattered". A `kind` says the thing itself.
- **It gives the transcript↔room join, which does not exist in either direction.**
  `claude_chat_messages` holds no event id and the room holds no message id, so "which room event is
  this row" and "which row is this event" are both unanswerable. That is what an operator redacting
  or editing a message, or reacting to one, would need to be attributable to anything.
- **It makes delivery idempotent for free.** Tagging a reply with its `agent_message_id` turns the
  room itself into the ledger: an adopting console can ask "have I already posted `msg_x`?" instead
  of keeping a durable side table for it. That is the piece the identity-based replay design
  (<cli_protocol_ownership.md>) would otherwise have to invent, and it composes exactly — the frame
  is recognisable on the wire, and its delivery is recognisable in the room.

Three cautions. **Custom content is public** to everyone in the room and federates, so it carries
ids and kinds, never anything sensitive. **A redaction strips content**, so metadata does not
survive one — a redacted status line is untagged by definition. And **the existing history has no
tags**, so every reader has to tolerate absence: an untagged `m.text` from the operator is
conversational, an untagged `m.notice` is not, which is exactly today's rule kept as the fallback.

The inbound direction wants the same treatment and is half-done: `_as_prompt` renders
`[{event_id}] {body}` so the agent can cite a message back, but those ids live only inside the
prompt string. Which room events produced a prompt is therefore recoverable only by parsing prose —
structured on the way out, unstructured on the way in.

## The rate limit is guessed at, never handled — and the guess only covers the calmest sender

`STATUS_EDIT_INTERVAL_SECONDS = 5.0` is the console's whole answer to Synapse's per-room limit, and
three things are wrong with it.

**It drops instead of debouncing.** `show_status` returns early inside the floor without keeping the
value it refused, and `_TurnStatus` has already recorded that state as shown — so the update is lost
rather than deferred, and the line reads stale until the next change (above). A correct trailing-edge
debounce keeps the latest pending value and flushes it when the floor passes. That is the fix for the
status line specifically.

**But a debounce is only correct for latest-wins state**, and most of what the console says is not
that. A status line is latest-wins: superseding it loses nothing. A reply, a bootstrap line, a
`holding N message(s)` — dropping any of those loses information. So the room does not want one
debounce, it wants a **paced send queue**: FIFO for anything that must arrive, with the status line
collapsing into a single pending slot inside it. One pacer per room, because the limit is per room
across every kind of send — which the current design cannot express, since the pacing lives inside
the one method that edits the status line.

**And the loudest paths are the unpaced ones.** The status line is the only sender that paces itself.
Unpaced: `reply` — now once per assistant message, so a turn that says three things posts three
messages back to back; `announce`; `_report_holding`; the redaction in `clear_status`; and worst,
the bootstrap narration, which is **one notice per line** of `SetupOutput` (`transport.py` splits
the runner's raw chunks and awaits `on_progress` per line) as fast as the bootstrap writes them.
The calmest sender has all the pacing and the burstiest has none.

**Nothing reads the server's own answer.** `_unwrap` turns any `ErrorResponse` into
`MatrixError(f"{status_code}: {message}")`, so a 429's `retry_after_ms` — the homeserver telling us
exactly how long to wait — is discarded at the boundary. nio can retry these itself
(`AsyncClientConfig(max_limit_exceeded_retries=…)`) and the config sets only `encryption_enabled`
and `request_timeout`, so it does not. The five seconds is therefore open-loop: a guess at a limit
whose real value is `rc_message` on that homeserver (Synapse's default is 0.2/s with a burst of 10,
unless tuned), never checked against what the server actually said.

Fixing that is the cheap half — honour `retry_after_ms`, or let nio do it — and it is also what
turns the queue above from a guess into a control loop.

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
as an audit log beside it? Today it is written as if it were primary and read as if it were derived.

A second backend decides this, and decides it against the projection: the rollout is one agent
protocol's wire, so it cannot be the record a Codex or direct-API session shares. Transcript primary,
rollout per-backend evidence — which means the double write and `agent_message_id` are what to remove
here, and the calls move into a table rather than into a frame parse (both above).

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
- **`MatrixSyncService._sent_event_ids` can never match.** It is an unbounded in-memory set of every
  event the console has sent, checked in `_serviced` so the console does not treat its own posts as
  input. But `MatrixClient._messages` has already dropped every event whose `sender` is the bot —
  which is every event in that set, since they are all sent as the bot. The second filter is
  unreachable, and it is the one that costs memory and is lost on restart. The sender check is the
  real mechanism, and it is one layer down with a comment naming R1.5.

## Streaming the answer into the room — [later]

The SPA watches an answer form, because `update_assistant` rewrites the message row per delta and
SSE carries it. Matrix hears nothing until an assistant message completes. Wanted eventually, at low
priority — and two facts decide the design before taste does.

**Matrix cannot stream at delta rate.** `STATUS_EDIT_INTERVAL_SECONDS` is five seconds, and its
comment says why: the floor is Synapse's per-room rate limit and what a person can read, not how
fast the agent produces text. Every edit is also a real, permanent event in the room's timeline,
federated and replicated, where an SSE frame is not. So what is achievable is a coarse "the text so
far" refresh every few seconds, not token-by-token — worth knowing before deciding it is worth
building, and part of why it is correctly low priority.

**The obvious implementation would poison the re-awakening history.** Editing the reply itself means
the answer's final text lives in the last `m.replace` of a chain, while the original event holds the
first chunk. `MatrixClient.recent_messages` — which feeds the last twenty messages into a
replacement session's system prompt (R3.3a) — keeps every `RoomMessageText` and deliberately keeps
Haku's own, and an edit of an `m.text` reply _is_ an `m.text` event. The trick that excludes status
chatter for free does not apply: notices are excluded because `m.notice` parses to a different nio
class, and a reply's edits do not. So a streamed answer would fill the history read with twenty
partial drafts of one message, each carrying the `* ` fallback prefix, unless that read learns to
resolve edit chains and take the latest replacement per original.

**Which is the argument for streaming into the status line's mechanism instead of into the answer.**
`show_status`/`clear_status` already create one notice, edit it under a rate limit, and redact it
when the turn ends. Streaming prose is that same pattern with different content: the preview is a
notice, so it is excluded from history by the existing rule; the final answer is still posted once as
a clean `m.text`; nothing has to resolve edit chains; and a console that dies mid-stream leaves a
stale preview rather than a permanently truncated answer. It also unifies with the status line rather
than competing with it — one line that says what Haku is doing, showing prose while there is prose
and tool names when there is not.

The cost is a visible seam: the preview disappears and the answer appears, where editing in place
would grow the message a chat client already renders as one. That is the real trade, and it is a
question about the room's feel rather than about the machinery.

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

**Where it should be deleted to has changed**: into a `tool_call` table written by the backend
adapter, not into a parse of the Claude CLI's frames, which only sessions on that backend have. See
"a second agent backend" above — the synthesized-message case dissolves there too, since a
synthesized message and an observed one both produce adapter events.

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
