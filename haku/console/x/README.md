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
to. **Being at the right level is not the same as being neutral, and this module is where that bit:**
it sat here while still matching on Claude's own frame `type`, its `system` subtypes and its content
blocks, so a second harness got no status line from the module the rule had already placed above
harnesses. It reads `conversation_events.py` now. `sandbox_claims.py` is the mirror case: `claude-`-prefixed claim names, but it is Kubernetes
provisioning and would serve any harness. The frame vocabulary was the case that was genuinely
both, and it is now two modules: the CLI's own top-level `type` values and the readers that pick a
value out of one are `claude_code/frames.py`, while `setup_output.py` holds the bridge envelope's
`kind` and the row the console authors under it.

**One column still carries both**, which no placement fixes: `session_frames.kind` takes the CLI's
`type` from `RolloutRecorder` and `setup_output` from the progress reporter, so `session_store.py`
names two of the CLI's kinds in SQL predicates and imports them across that line — the same
direction `claude_code/projection.py` is already imported in. Stage 2 of
<../../plans/chat_runtime_projection.md> gives the CLI's type its own column, which is what retires
those predicates.

## `session_store.py` and `session_runtime.py` — the rows, and the turn loop over them

The shared substrate: session, message and turn rows, Postgres `LISTEN`/`NOTIFY`, the
SandboxClaim, the runner WebSocket bridge, and the `handle_runner` turn loop. Also the SPA chat
surface's own HTTP routes and SSE stream, which is the older of the two experiments.

**The line between the two files is the transaction.** `SessionStore` owns the SQLAlchemy sessions,
so a method whose job is "these writes commit together or not at all" is in `session_store.py` — the
outbox write and the turn-state transitions included, because each of those commits beside the
effect it describes and moving one out would be the drop it exists to prevent. What is left in
`session_runtime.py` is what drives one turn against a CLI: the client wiring, the room and status
plumbing, the sandbox lifecycle, the `ChatFrontend` port and the SPA's own routes. A second channel
inherits the store unchanged, which is why it stays at the runtime level and imports nothing under
`channels/`.

**The tables are `sessions` and `session_*`.** Migrations `0040` and `0041` are the expand and
contract of the rename off `claude_chat_*`, which is why the lineage has two revisions for it.

**A turn is a row, and it is a range over the frame log.** `next_prompt` dequeues a prompt and
opens `session_turns` in one transaction; `_run_turn` is that turn's span and closes it on
every exit, keeping what the exchange cost. At most one
turn per session is open (a partial unique index), and that is the single question behind three
answers: whether a prompt may be admitted, whether there is anything to abort, and whether the
SPA is shown `responding` — which the session view derives rather than reading off a column.
So an open turn on a session nothing is renewing is not a leak; it is the record of an exchange
whose replica went away before anything could close it.

**And the row carries the turn's state, not only its extent.** `assistant_message_id` is the
message being streamed into, `said_anything` whether one has completed, `queued_reply` whether the
room's outbox holds a reply from this turn — each written in the same transaction as the effect it
describes, so none of them can claim something that did not commit. `_run_turn` reads them at the
top of every turn (`turn_state`), which is why adopting a half-finished exchange is a read rather
than a reconstruction: `adopt_open_turn` says _which_ turn, and the row says how far it got
(<../../plans/chat_runtime_projection.md> § stage 3). **Gotcha:** `queued_reply` is the outbox row
existing, deliberately not `sent_at` — an unsent row still means the room is owed that text, so a
turn that re-queued it would post the answer twice — and `said_anything` is a separate column
rather than the same one read twice, because a session with no room queues nothing.

**What the exchange cost is not recorded at all.** `session_turns` still has
`input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd` and `duration_ms`, and nothing
writes or reads them — they are mapped only until the release that stopped writing them has
converged (the tombstone on the columns names the sequence). The numbers stay recoverable: they
were read off the `result` frame's payload, which is in `session_frames` verbatim and inside the
turn's own frame range.

**The rollout is recorded by `RolloutRecorder`, a `FrameSink` the protocol client calls.** Every
frame either way, both channels, verbatim — the control channel included, since an interrupt that
did not take is diagnosable from nothing else. **Deltas included** — a log with a hole in it cannot
be folded over (<../../plans/chat_runtime_projection.md> § stage 1), so "do not bury the reader" is
answered at the read instead: `read_frames` leaves `stream_event` out of its default view. They are
also what makes an interrupted turn's half-answer survive: an answer no `assistant` frame ever
completed is in the log as the deltas that wrote it.

**A row carries two numbers, and only one of them is this end's.** `frame_seq` is Postgres's
`Identity` and is still the log's ordering, its keyset cursors and every reference to a frame.
`runner_seq` is the number the **runner** put on the frame as it went on the wire, recorded
alongside — dense over everything one runner sent, where `frame_seq` is sparse by design. Nothing
orders or pages by it. What reads it is `highest_runner_seq`, the session's **resume cursor**: the
console sends it on `start` (`ClaudeLaunch.resume_from`) and the runner replays only what is above
it, instead of offering its whole window for `frame_uid` to sort out — which the frame classes
carrying no agent-assigned id escape. Two properties worth knowing before changing it:

- **Per session, not per connection.** Two consoles can be adopting one runner's window during a
  roll, so both compute the cursor from the same rows and agree. The runner treats it as a floor
  (`max(next, resume_from + 1)`), never as an assignment.
- **It can legitimately sit below what the console holds, and never above.** A `setup_output` frame
  is numbered by the runner and recorded by a different path — one frame decodes into however many
  complete lines it finished — so its number is not on any row. The cost is a re-sent frame the log
  already has, which the `frame_uid` dedup refuses. **This is also why there is no gap detection
  yet**: a hole in the recorded numbers is what narration leaves behind, and the runner's replay
  window retains only replayable frames, so an adopted connection's own sequence is sparse too.
  "A hole means loss" becomes true at the release that makes this the log's ordering
  (<../../plans/chat_runtime_projection.md> § 2b, R2), not before.

Both surfaces run on it at once. They are ordinary separate sessions — separate rows,
separate sandboxes — so a browser conversation and the Matrix conversation coexist rather
than contend. **Gotcha:** that also means two live sandboxes, and only the Matrix one
announces itself, so the browser one is the easy one to forget you are paying for.

Delta streaming (`StreamEvent`) exists for the SPA alone. The Matrix path forwards whole
assistant messages, each as it completes (R11.1), so if the SPA view is ever retired that
machinery goes with it.

### What has been lifted out of it

Each is a module nothing in `session_runtime.py` reaches into — so the store, the service and
the turn loop kept their shape while the file lost the parts that never needed to be beside them
(<../../plans/chat_runtime_cleanup.md> § Anytime). `session_store.py` is not one of them: the
service calls it on every path, so that split is a seam and not a leaf.

| Path               | Role                                                                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `setup_output.py`  | The bridge envelope's `kind` and the sandbox-narration row the console authors under it.                                                                       |
| `session_views.py` | The read models the API returns for a session or a conversation, and the projection that assembles one out of the session row, its transcript and its rollout. |
| `room_status.py`   | The per-turn status driver: what the room is shown while a turn runs, and when. It is handed two coroutines and never learns which room it speaks to.          |

### The records a read hands back — `conversation_records.py`

What a conversation read produces: a session row, a rollout frame, a turn, a transcript entry,
and the cursors that page them. Here because the store is what produces them — the store used to
import them from <../tools/conversations.py>, the MCP server that reads it, which is the
dependency running backwards. The line the split draws is **record versus page**: what one read
produced is here, and how it is handed out — the `Page` envelope, the byte budget a page spends,
and the clipping that budget forces — stays with the tool.

**It is the one edge that runs stable → experimental**, so it is worth knowing before it
surprises: everything else outside `x/` that names a module inside it is the composition root
(<../app.py>). This module is a leaf of Pydantic models with no dependency of its own, so naming
it drags in no runtime — but if the tools catalog ever has to build without `x/`, moving this
module to the console level is the fix, not reinstating the models on the tool.

## Prompting a session the browser did not create

`POST /api/sessions/{session_id}/messages` and `/abort` ask **who owns the session**, never which
surface opened it: `enqueue_prompt` checks `operator_id`, `READY`, no open turn and no queued
prompt, and nothing in the route or the store reads `sessions.surface`. So the conversation detail
view carries a composer (`frontend/x/conversation_composer.tsx`) for any session it can read,
a room's included — the reply then goes wherever that session's channel sends replies, so a prompt
typed in the browser also lands in the room.

**A refusal is the case the surface has to render, not the send.** The route holds nothing:
`enqueue_prompt` refuses a prompt arriving mid-turn and refuses a second prompt while one is
queued, and the queued case leaves the session `READY`, so no disabled button can pre-empt it.
Both come back as `409`, which the client raises as `PromptRefused`; the composer keeps the
operator's text, because until the console accepts a prompt its only copy is in that box.

## The frame inspector — reading the record the transcript is a projection of

`session_messages` is a **lossy projection** of `session_frames`
(<../../plans/chat_runtime_projection.md> § stage 4), and a projection nobody can appeal is a
projection nobody can debug. So a conversation in the console carries a **Raw frames** button, and
`GET /api/sessions/{session_id}/frames` is what it reads: one page of the rollout, in wire
order, each frame's payload whole. Frontend: `frontend/x/session_frames_page.tsx`, at its own route
(`/_console/sessions/<id>/frames`) so a frame is something an operator can link to — under the
session rather than the conversation, because a conversation outlives its sessions and has several
while the frames belong to exactly one.

**It is the one surface in the console that shows a backend's own shapes, and it says whose.** Three
conditions keep that from being a hole in the layering: it is addressed separately, so no channel can
consume a provider payload by accident; it is never load-bearing, so a reader that cannot reach it
loses a debugging affordance and nothing else; and it is **labelled as one backend's wire** rather
than as the conversation. The third is the one a surface can quietly drop, so the label is written
where each reader is — the route's description, `SessionFrameView`, the page's own subtitle, and
`haku_conversations`' `read_rollout` / `read_frame`. A second backend makes "the agent protocol" an
outright lie, but it is already misleading with one: every other read the console serves is the
neutral vocabulary, so an unlabelled frame reads as the conversation itself.

Decisions worth knowing before changing it:

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
- **A frame the fold had no branch for says so.** Each row carries its own `Projection.unprojected`,
  badged in the inspector — the transcript is silently missing whatever the adapter did not map, and
  the key is the frame class to go and write a branch for. Diagnostic only: no rendering, notice or
  delivery decision reads it. The keys stay the backend's own class names, because the adapter is
  the one component allowed to be provider-shaped; neutralizing them would delete the answer.

**Where per-message provenance lands** (`agent/claude-frame-provenance`, #4105): a message's
inclusive `frame_seq` range is a bound on the same query — `before_seq` is already its upper half —
so linking one message to its frames is a filter over this view, not a second read path. It is
deliberately not guessed at here: the join key the transcript has today (`agent_message_id`) is
missing on thousands of production rows, which is why that change exists.

## The neutral projection — what reads it

`conversation_events.py` is the provider-neutral vocabulary a conversation is read as — text,
messages, reasoning, a tool-call lifecycle, a completed turn — and
`claude_code/projection.py` is the reducer from Claude's frames into it. Together they are the
one interpreter that <../../plans/chat_runtime_projection.md> § stage 4 replaces four with.

**The two halves sit on different levels, which is the placement rule doing its job.** The
vocabulary names no backend and every surface renders it, so it stays at the runtime level; the
adapter cannot be written without knowing what `assistant`, `stream_event` and `tool_use_result`
are, so it lives under the harness directory beside `claude_code/frames.py`, which is where those
names are spelled. A second backend adds a sibling adapter and touches neither the vocabulary nor
its readers.

**The reducer's own contract is in `claude_code/projection.py`** — what `project`, `finish` and
`project_log` each mean, why a batch boundary is not an ending, and the wire facts every rule in
it answers. It is not restated here; what follows is who reads it and what is not yet on it.

**Two readers are on them.** `haku_conversations.read_transcript` reads a stored session:
`SessionStore.read_transcript` calls `project_log` and `transcript_entries.py` maps the result
onto the read models in `conversation_records.py`. And the live path reads them too.

**All four interpreters are gone.** `_run_turn` projects each frame as it lands and acts on the
events, so the live path no longer knows that `assistant`, `stream_event` and `result` exist;
`coarse_status` reads a run of events rather than a frame, which is what stops the status driver
being one of the interpreters it is supposed to sit above; adoption replays from the cursor rather
than reconstructing; and the read path's own parser — `rollout_calls`, which re-derived every tool
call and every result on every request — is deleted, because the events are rows.

### The events, stored — `session_events`

`session_events.py` maps a `ConversationEvent` onto a row and `apply_frame` writes it inside the
transaction that moves the cursor, so a row exists exactly when the cursor says its frame was
projected. The same module maps the console's own facts about a session onto rows, written in the
transaction that makes each true, and the operator's prompt onto one written in `enqueue_prompt`'s. What the table holds that nothing else does is a **tool call's answer**, which
used to exist only as frames that `session_views.rollout_calls` re-parsed on every read.

- **A message is found by frame range**, not by the agent's id for it — `session_messages` records
  the span it was built from and an event's own span falls inside it, so the join needs nothing the
  wire may have omitted.
- **Provenance is a column, and `NOT NULL`.** `frame_range` carries both ends and `authored`
  carries neither, which the table's own constraint makes the only two possibilities — the
  requirement #4143 could not put on `session_messages`, where NULL means two things.
- **The two kind enums split the stream by where a row came from, not by what it is about.**
  `ConversationEventKind` is what a fold of recorded frames produced, so every such row carries a
  frame range; `AuthoredEventKind` is what no frame carries and the console alone witnessed. The
  frame log is the record of runner↔console traffic and nothing else (operator, 2026-08-16), so a
  fact that crossed no wire has to be named on the authored arm however conversational it reads.
  Such a row need not name a turn — a session that died before reaching one is exactly the case the
  arm exists to record — but it may: `turn_aborted` does, because what the operator stopped was the
  exchange.
- **An abort is one of those rows, and the room's "aborted" line is its projection.** `end_turn`
  writes the event under the same lock that closes the turn and the channel announces afterwards —
  record, then show, so a crash in that window cannot leave a notice nothing recorded.
- **So the operator's prompt is an `AuthoredEventKind`, not a lifecycle claim about it.** A prompt
  is conversation — half of what a transcript renders — but it is accepted before it is asked: a
  session holds no sandbox until a prompt buys one, so at acceptance there is no runner to send it
  to, and a session that ends before the prompt is claimed never sends it (`PromptFate.LOST`). It
  is turn-less because admission refuses a prompt while a turn is open. Recording it once here
  costs no duplicate when it does go out, because the fold projects an outbound prompt to nothing —
  the console already holds the text. Without the row, `event_seq` addresses only the agent's half
  of a transcript.
- **Two members of the vocabulary have no row.** A `TextDelta` is an increment of prose the
  completed message carries whole; a `TurnCompleted` is the `session_turns` row, which already
  holds the exchange's outcome and its bracket.

**A status source did not survive the neutral vocabulary, and it is worth knowing before it is
missed.** `coarse_status` used to render the prose the CLI writes on `system/task_started` — a
background command's own command line, better than anything the console could reconstruct. That
prose is Claude's concept keyed by Claude's identifiers, so nothing in the vocabulary carries it and
`task_started` now lands in `Projection.unprojected`. A turn that would have shown it shows
`writing` instead, and the frames stay in `session_frames` for a reader that wants the detail.

**Two `session_events` kinds outlive their events.** `ConversationEventKind.ACTIVITY_STARTED` and
`ACTIVITY_COMPLETED`, and `ck_session_events_kind` with them: rows written by earlier releases exist,
`kind` is parsed rather than read as text, and a member removed while its rows survive makes reading
one raise rather than degrade. Deleting the rows and narrowing the constraint is a later migration.
Until then `reprojection.check_session` reports a `RowCountMismatch` on any old turn holding one,
because the fold no longer produces what the row records — a true report, not a false alarm.

### The cursor — where the fold resumes, and what makes its effects exactly-once

`sessions.projected_frame_seq` is the `frame_seq` of the last frame whose projected effects are
committed, and `SessionStore.apply_frame` is the one transaction that writes both: the message row,
the room's outbox row, the turn's state and the cursor land together or not at all. So a replica
that dies leaves the session naming the last frame that took, and its replacement redoes exactly
the frames whose effects did not.

**The payoff is that adoption stops being a second reading of the log.** `adopt_open_turn` hands
back the recorded frames past the cursor and `handle_runner` feeds them to the turn loop ahead of
the live stream (`_replaying`), so a turn's remaining frames go through the same call whether they
have just arrived or arrived at a replica that is gone. A turn whose ending is among them closes
without the socket being consulted, which is what "the `result` is logged but `end_turn` never ran"
used to be a separate question about.

Four things to know before changing it:

- **The turn loop still seeds a fresh state per frame** (`frame_projection.projected` calls
  `project_log`), which is also what the cursor currently rests on: the fold carries nothing across
  a frame boundary, so the state at any cursor position is the empty one and a position is the
  whole of what resuming needs. Threading one state across the turn — a two-line change that was
  tried and breaks two things in the _loop_, both written up in `projected`'s docstring — is what
  would make that false, and `session_turns.first_frame_seq` is the answer waiting for it:
  re-project one turn.
- **`next_prompt` anchors the cursor** at the frame before the turn it opens, in that same
  transaction. That is what lets adoption tell a position inside this turn from one left behind by
  a writer that does not advance it — a replica on the previous image, for the length of a roll —
  and refuse the second (`projected_frame_seq < first_frame_seq - 1`).
- **`end_turn` carries the cursor past the frame that ended the turn**, not `apply_frame`. The
  turn's last word is written between the two, so advancing when that frame was projected would
  put the cursor ahead of writes still to come.
- **A turn is resumed from its cursor or not at all.** A cursor from before the turn — or none at
  all — is a position re-projecting from would redo effects that did commit, so `adopt_open_turn`
  ends such a turn as failed rather than resuming it. No session that can still acquire a frame is
  in that state (<../debug/2026_08_16_legacy_purge.md>).

**`read_transcript` has no cursor and is not this one.** It re-reads the session and seeds an empty
state per page, so it folds from the first frame every time; that is a read path with nowhere to
keep a position, not the durable one.

Four properties hold the design up, each stated where it is kept — break one and the rest stop
meaning anything:

- `project` is pure and deterministic (`projection.py`).
- One batch and any split of batches project alike (`Projection.then`).
- Every event carries provenance, and it is a union, so a rebuild cannot delete an authored event
  (`conversation_events.Authored`).
- The default branch is counted, not dropped (`Projection.unprojected`) — and the count is read:
  per session as `read_transcript`'s `unreadable`, per frame as the inspector's `unprojected` badge.

Before changing the adapter, read <../debug/frame_shape_census.md>: every rule in it is a measured
fact rather than a reading of `protocol.md`, so what looks like belt and braces mostly is not.

### Re-projecting a stored session — `reprojection.py`

`check_session` re-projects a recorded session's frames and aligns the result against
`session_events`, returning per turn either `Agrees`, `Drifted` with its findings, or `Skipped`
with the era it cannot speak about. It is a function rather than a command, and has no caller in
the tree: the backfill it was written for pointed the rows the purge deleted.

- **It folds through `frame_projection.projected`**, which is why that function is not the turn
  loop's private one. A checker driving `project_log` over a whole session instead would merge the
  frames sharing one `message.id` and cut its deltas from completed blocks — a different, equally
  correct, event sequence, and it would report drift everywhere.
- **It aligns by frame.** One frame's rows are written in one transaction and each event's range is
  `(frame_seq, frame_seq)`, so which rows a frame owns is a lookup. It reads a turn's rows on the
  `frame_range` arm only, so the authored category is out of scope — which is what stops a rebuild
  from deleting what it cannot re-derive. That filter is load-bearing now that `turn_aborted` names
  a turn.
- **One era bounds what it may speak about**, per turn and named by `SkipReason`: a turn whose
  frames the cursor never reached (#4178's era). A turn with frames and no rows at all is not that
  — nothing writes a projected frame without its rows — so it is drift, and is reported as drift.

### Recording a session as a fixture — `frame_export.py`

`claude_code/testdata/diverse_session.jsonl` was captured off the CLI's stdout in a throwaway
directory, so it can only ever hold a session somebody ran for the purpose. The shapes still
unrecorded are the ones a console session produces — a `Task` subagent, a backgrounded `Bash`, the
`BashOutput` loop watching it — and those exist in `session_frames`. `frame_export.py` is the
second route to the same file format:

```bash
bb run //haku/console/x:frame_export_bin -- \
    --session <uuid> --output haku/console/x/claude_code/testdata/<name>.jsonl
```

`--database-url` comes from `HAKU_CONSOLE_DATABASE_URL`. Check the file in with a `data` entry on
the test that reads it, as `test_diverse_session` has.

- **The exported file is a proposal, not an artifact.** Read it before committing. Redaction
  (`claude_code/redaction.py`) is fail-closed by key — identifiers are pseudonymised so equality
  survives, a short list of discriminators and tool names is kept, and every other string elides to
  its own length — so what it keeps is a list rather than a judgement about one session, and the
  review is the other half of that.
- **What it exports is what the fold reads**, so the console's own `setup_output` rows are gone
  and a record's index is its `frame_seq` rather than the table's number. Both differences are
  written up in `frame_export.py`.
- **The composed half is `test_agents_and_background.py`.** It pins what the projection does with
  each of those shapes today, built from `claude_code/testing/wire.py`; the subagent frames in it
  are a hypothesis drawn from `protocol.md` and the census, which a capture is what settles.

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

Behaviours worth knowing before reading the code:

- **A produced reply is a row, not a call.** A turn writes it in the same transaction as the
  assistant message it copies, and `RoomOutboxDrain` — one replica, under the `MXOB` advisory
  lock — claims the oldest, queues it into the pacer, and marks it `sent_at` only once
  `room_send` has returned (the drops this closes:
  <../debug/message_drops.md>). Everything else the console says — the
  status line, lifecycle and rejection notices, bootstrap narration — stays on the pacer's
  in-process queue, because a notice describing a moment is not worth redelivering ten minutes
  later. Two rules the drain is deliberate about: a failed reply **halts** the queue for its
  backoff rather than being overtaken, and the one row stepped over is one out of
  `MAX_SEND_ATTEMPTS`, kept unsent with its `last_error` and logged loudly rather than deleted.

- **A rejected batch is not queued anywhere.** `enqueue_prompt` only accepts on a ready session
  with no turn open and nothing pending, and a refusal is the answer: the room is told the messages
  were not delivered and what to wait for, and the watermark advances past them so the homeserver
  does not offer them again. Nothing queues behind a running turn, and there is one sync position
  rather than two. **Admission is that one transaction's alone**: `MatrixTurns.offer` asks no
  status question of its own, because an answer read outside `enqueue_prompt`'s
  `SELECT … FOR UPDATE` could only agree with a decision that had not been made yet. It turns the
  refusal into a `PromptRejected` carrying the reason and the row that records it — `PromptRefusedError`
  for a session that cannot take the batch, `KeyError` for a session row that has gone.
- **The rejection is a row, written with the watermark.** `AuthoredEventKind.PROMPT_REJECTED`
  carries the reason and the text, and `MatrixSyncStore.advance` inserts it in the transaction
  that acknowledges the batch — so a crash cannot acknowledge a message while losing both the
  record of it and the operator's only account of what happened. The room notice is a rendering of
  that row. The one rejection with no row is a room whose session has not been provisioned yet:
  a `session_events` row names a session, and there is none to name.
- **An accepted batch is acknowledged at once, and that has a cost.** A prompt is a row on the
  session that took it, so a session ending before it claims that prompt leaves the message
  unanswered by anything (<../debug/message_drops.md> I3). What carries it forward is the
  replacement session's waking context: `RoomTranscript.recent` reads every session that served
  the room, so an accepted-and-unanswered prompt is in the history the replacement is handed.
  Holding the acknowledgement until the turn ended is what used to close that window, and it is
  what the rejection ruling removed.
- **An event Haku cannot read is announced, not held.** `m.text` and `m.emote` are prose and are
  serviced; an `m.image`, `m.file`, voice memo, or an msgtype invented after this release is
  carried out of the sync as an `UnmappableEvent`, said out loud in the room, and then
  acknowledged. Refusing the batch instead would never converge — nothing about an already-sent
  screenshot changes, so it would wedge ingress against every later message. It is recorded the
  same way a rejection is, one `AuthoredEventKind.UNREADABLE_INPUT` row per event.
  `m.notice` is neither serviced nor announced: it is the
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
a room only through the `ChatFrontend` port and the `room_id` string a session records — never
`matrix-nio` — so what they cover (the turn loop, the outbox row, provenance, adoption, the abort
drain, prompt fate) is what a second channel inherits. A message _arriving_ from a room and becoming
a turn is the crossing itself and cannot be written without both halves, so `MatrixTurns.offer` is
tested in `channels/matrix/test_session.py`, beside the module that defines it.

### The stand-ins live in `testing/`

Everything a test stands something up _with_ is a module in `testing/` — the claim stand-in
(`testing/recording_claims.py`), the stub `claude` (`claude_code/testing/stub_claude.py`), its
frames (`claude_code/testing/wire.py`), the
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

What it is deliberate about:

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
- `Session`, `SessionMessage`, `MatrixAccessToken`, `MatrixSyncWatermark`, and `MatrixConversation` in
  <../database_schema.py>, plus their Alembic revisions — migrations are one lineage for the
  whole database.
- The `ChatFrontend` port is defined next to the service that calls it (`session_runtime.py`);
  the three methods the status driver drives are `StatusFrontend`, declared beside that driver in
  `room_status.py`, and a frontend satisfies both by implementing one port. The composition in
  <../app.py> is what ties a frontend to the sessions it serves.

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../../plans/chat_runtime_cleanup.md> and <../../plans/chat_runtime_projection.md> — what is
  still wrong and the order to fix it.
- [The runtime archaeology note](../debug/2026_08_16_runtime_archaeology.md) — which bug each
  invariant in `session_runtime.py` and `session_store.py` was written against, indexed by the line
  in the code that keeps it.
- `debug/` otherwise holds dated findings from one incident and is not maintained.
