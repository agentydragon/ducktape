# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

Two chat surfaces live here, deliberately. They are separate experiments over one piece of
session machinery, not a migration in progress: Matrix is not replacing the SPA view. Both are
headed for one subscription off one record (<../plans/conversation_layers.md>).

## The directory says which axis a module varies on

Three things vary independently here. _Matrix is just one of pluggable backends — channels_
(operator, 2026-08-16), and the harness directory is `claude_code/` rather than `claude/` because
the CLI harness and the model behind it are different axes (operator, 2026-08-15):

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
to. **Being at the right level is not the same as being neutral**: it reads `conversation_events.py`
rather than Claude's own frame `type`, which is what lets a second harness get a status line from a
module placed above harnesses. `sandbox_claims.py` is the mirror case: `claude-`-prefixed claim
names, but it is Kubernetes provisioning and would serve any harness. The frame vocabulary is
genuinely both, so it is two modules: the CLI's own top-level `type` values and the readers that
pick a value out of one are `claude_code/frames.py`, while `setup_output.py` holds the bridge
envelope's `kind` and the row the console authors under it.

## `session_store.py` and `session_runtime.py` — the rows, and the turn loop over them

The shared substrate: session, message and turn rows, Postgres `LISTEN`/`NOTIFY`, the
SandboxClaim, the runner WebSocket bridge, and the `handle_runner` turn loop. Also the SPA chat
surface's own HTTP routes, which is the older of the two experiments.

**The line between the two files is the transaction.** `SessionStore` owns the SQLAlchemy sessions,
so a method whose job is "these writes commit together or not at all" is in `session_store.py` — the
outbox write and the turn-state transitions included, because each commits beside the effect it
describes and moving one out would be the drop it exists to prevent. What is left in
`session_runtime.py` is what drives one turn against a CLI: client wiring, sandbox lifecycle and
the SPA's own routes. It records setup, answers, silence and turn state without holding a channel;
subscribers project those facts. A second channel inherits the runtime unchanged, which is why it
imports nothing under `channels/`.

`sandbox_allocation.py` is the channel-neutral reconciler between those halves. A prompt commits as
conversation-owned demand while its session is still idle; one `SBOX` leader finds that row and
calls the runtime only after admission has returned. Notification wakes make the common path fast,
and an oldest-first periodic sweep makes a lost notification or dead admitting replica a delay
rather than a dropped first message.

Session provisioning, setup narration and ending are authored into `conversation_event` in the
same transactions that make them true. Narration also writes a temporary `setup_output` frame for
readers from the previous release; the event is the durable fact and the frame is only a rollout
compatibility copy.

**Two families of table, and the split is the point.** `sessions` and `session_frames` are one
runner incarnation's — its lease, its claim, its wire log. `conversation_*` is what the thread
durably is, and outlives every session that runs it: <../docs/conversation_schema.md> is the design.
A channel keyed to the second reads on through a sandbox being replaced.

**A turn is a row, and it is a range over two logs.** `next_prompt` dequeues a prompt and opens
`conversation_turn` in one transaction; `_run_turn` is that turn's span and closes it on every exit.
The row brackets the exchange in the conversation's own `event_seq` and in the session's frame log,
which are different coordinates for the same stretch — the first is what a channel resumes from and
the second is what a re-projection re-folds. At most one turn per conversation is open (a partial
unique index), and that is the single question behind three answers: whether a prompt may be
admitted, whether there is anything to abort, and whether the SPA is shown `responding` — which the
session view derives rather than reading off a column. So an open turn on a session nothing is
renewing is not a leak; it is the record of an exchange whose replica went away before anything
could close it.

**How far the turn got is not on the row.** What it is streaming into is its one open message item,
and whether it said anything is whether it has a completed one (`turn_state`), so there is one place
either fact can be wrong rather than two that can disagree. Adopting a half-finished exchange is
still a read — `adopt_open_turn` says _which_ turn, and the items say how far it got — and a fold
resuming mid-message finds the item its predecessor left open rather than opening a second.

**The rollout is recorded by `RolloutRecorder`, a `FrameSink` the protocol client calls.** Every
frame either way, both channels, verbatim — the control channel included, since an interrupt that
did not take is diagnosable from nothing else. **Deltas included**, because a log with a hole in it
cannot be folded over; readers are bounded by row and byte budgets rather than by classifying the
native payload. Deltas are also what makes an interrupted turn's
half-answer survive: an answer no `assistant` frame ever completed is in the log as the deltas that
wrote it. Tool argument deltas are load-bearing too: some Claude Code builds can execute a call and
return its result before writing the completed `assistant` block, so the stream's block start,
partial JSON and stop are what first make that call addressable.

**A row carries two numbers, and only one of them is this end's.** `frame_seq` is Postgres's
`Identity` and is still the log's ordering, its keyset cursors and every reference to a frame.
`runner_seq` is the number the **runner** put on the frame as it went on the wire, recorded
alongside — dense over everything one runner sent, where `frame_seq` is sparse by design. Nothing
orders or pages by it. It is unique per session and is the replay identity for native frames,
including deltas and control responses with no payload-level id. `highest_runner_seq` is the
session's **resume cursor**: the console sends it on `start` (`HarnessLaunch.resume_from`) and the
runner replays only what is above it. Two properties worth knowing before changing it:

- **Per session, not per connection.** Two consoles can be adopting one runner's window during a
  roll, so both compute the cursor from the same rows and agree. The runner treats it as a floor
  (`max(next, resume_from + 1)`), never as an assignment.
- **It can legitimately sit below what the console holds, and never above.** Setup narration's
  compatibility frame is minted by the console with no `runner_seq`. **This is also why there is no
  gap detection yet**: narration leaves holes in the recorded runner numbers, and the runner's replay
  window retains every native harness frame but not rendered setup narration. "A hole means loss"
  is therefore not yet a general database invariant across both bridge classes.

Both surfaces run on it at once. They are ordinary separate sessions — separate rows,
separate sandboxes — so a browser conversation and the Matrix conversation coexist rather
than contend. **Gotcha:** that also means two live sandboxes, and only the Matrix one
announces itself, so the browser one is the easy one to forget you are paying for.

### What has been lifted out of it

Each is a module nothing in `session_runtime.py` reaches into
(<../plans/conversation_layers.md> § 9). `session_store.py` is not one of them: the service calls it
on every path, so that split is a seam and not a leaf.

| Path               | Role                                                                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `setup_output.py`  | The bridge envelope's `kind` and frame shape retained temporarily as the setup-narration compatibility copy.                                                   |
| `session_views.py` | The read models the API returns for a session or a conversation, and the projection that assembles one out of the session row, its transcript and its rollout. |
| `room_status.py`   | The per-turn status driver: what the room is shown while a turn runs, and when. It is handed two coroutines and never learns which room it speaks to.          |

### The records a read hands back — `conversation_records.py`

What a conversation read produces: a session row, a rollout frame, a turn, a transcript entry, and
the cursors that page them. Here rather than in <../tools/conversations.py>, the MCP server that
reads it, because the store is what produces them. The line the split draws is **record versus
page**: what one read produced is here, and how it is handed out — the `Page` envelope, the byte
budget a page spends, and the clipping that budget forces — stays with the tool.

**It is the one edge that runs stable → experimental**, so it is worth knowing before it surprises:
everything else outside `x/` that names a module inside it is the composition root (<../app.py>).
This module is a leaf of Pydantic models with no dependency of its own, so naming it drags in no
runtime — but if the tools catalog ever has to build without `x/`, moving this module to the console
level is the fix, not reinstating the models on the tool.

## Prompting a session the browser did not create

`POST /api/sessions/{session_id}/messages` and `/abort` ask **who owns the session**, never which
surface opened it: `enqueue_prompt` checks `operator_id`, `READY`, no open turn and no queued
prompt, and no session records which surface opened it — which channels hold a conversation is
`chat_attachment`. So the conversation detail
view carries a composer (`frontend/x/conversation_composer.tsx`) for any session it can read,
a room's included — the reply then goes wherever that session's channel sends replies, so a prompt
typed in the browser also lands in the room.

**A refusal is the case the surface has to render, not the send.** The route holds nothing:
`enqueue_prompt` refuses a prompt arriving mid-turn and refuses a second prompt while one is
queued, and the queued case leaves the session `READY`, so no disabled button can pre-empt it.
Both come back as `409`, which the client raises as `PromptRefused`; the composer keeps the
operator's text, because until the console accepts a prompt its only copy is in that box.

## The frame inspector — reading the record the transcript is a projection of

`conversation_item` is a **lossy projection** of `session_frames`, and a projection nobody can
appeal is a projection nobody can debug. So a conversation in the console carries a **Raw frames** button,
and `GET /api/sessions/{session_id}/frames` is what it reads: one page of the rollout, in wire order,
each frame's payload whole. Frontend: `frontend/x/session_frames_page.tsx`, at its own route
(`/_console/sessions/<id>/frames`) so a frame is something an operator can link to — under the
session rather than the conversation, because a conversation outlives its sessions and has several
while the frames belong to exactly one.

**It is the one surface in the console that shows a backend's own shapes, and it says whose.** Three
conditions keep that from being a hole in the layering: it is addressed separately, so no channel can
consume a provider payload by accident; it is never load-bearing, so a reader that cannot reach it
loses a debugging affordance and nothing else; and it is **labelled as one backend's wire** rather
than as the conversation. The third is the one a surface can quietly drop, so the label is written
where each reader is — the route's description, `SessionFrameView`, the page's own subtitle, and
`haku_conversations`' `read_rollout` / `read_frame`. Every other read the console serves is the
neutral vocabulary, so an unlabelled frame reads as the conversation itself.

Decisions worth knowing before changing it:

- **The first page is the tail, and the cursor walks backwards.** `read_operator_frames` is the
  reverse keyset of `read_frames`, which the MCP reader uses forwards. The frames an operator opens
  this for are a session's last ones, and paging forward from frame one to reach them is what would
  make a long session expensive to debug. Each page still comes back in wire order.
- **The inspector does not classify native JSON.** Every native harness frame is shown in wire
  order, including deltas and payloads with no conventional discriminator. The only filter is
  Haku's outer bridge class; setup narration is available explicitly and through its dedicated
  view. Provider-aware summaries belong in provider tooling, not this generic surface.
- **Nothing is clipped, and the page size is what bounds the response.** The MCP reader clips a
  frame to a context budget; here the wire is the answer, so clipping would be the lossy projection
  again one level down. The browser's other cost is drawn down instead: a payload builds its
  syntax-highlighted editor only once it nears the viewport (`frontend/code_block.tsx`).
- **Unreadable counts belong to the projected transcript, not a raw row.** Whole-log projection
  reports classes the selected integration did not map; the inspector itself displays exact JSON
  without inventing a native kind or folding one frame in isolation.

**Where per-message provenance lands** (`agent/claude-frame-provenance`, #4105): a message's
inclusive `frame_seq` range is a bound on the same query — `before_seq` is already its upper half —
so linking one message to its frames is a filter over this view, not a second read path. It is
deliberately not guessed at here: the join key the transcript has today (`agent_message_id`) is
missing on thousands of production rows, which is why that change exists.

## Asking one session how its sandbox came up

`GET /api/sessions/{session_id}/provisioning` is the claim/Sandbox/Pod/runner graph
(`sandbox_claims.py`), read live off the cluster for **one session in whatever state it is now**.
Addressed at a session for the same reason the frame inspector is: a conversation outlives its
sessions and each of them got its own sandbox, so the session that died has an account only it can
give. `GET /api/conversations/{conversation_id}` still nests the same view, and deliberately only
while that session is provisioning — that read is what a follow's every update is assembled from,
at up to twice a second while a turn streams, and a cluster read per update is not worth paying for
a question already answered. Both go through one `_observed`, so the two surfaces cannot disagree
about what the cluster said; what differs is when each asks.

**Idle is explicit absence.** An idle session has never requested a sandbox, so its nested
provisioning view is `null` and the session-addressed response carries `sandbox=None`. Once
allocation commits, Kubernetes not having the requested claim (never created, ambiguously created
then cleaned up, or reclaimed after the session ended) is `claim_absent`. A cluster that could not
be read at all is `observation_error` on an otherwise empty view. These are distinct answers:
"never requested", "requested but absent", and "could not observe".

**One observation is reused for `OBSERVATION_TTL`.** A poll costs up to three Kubernetes reads, and
this is an operator-facing GET a browser watching a sandbox come up will poll — so the API server
pays a bounded rate rather than the operator's refresh rate. A failed observation is remembered like
a successful one, so an API server that is down is not asked harder than one that is up. The view
carries the `inspected_at` it was taken at, so a reader can see how old its answer is.

## The neutral projection — what reads it

`conversation_events.py` is the provider-neutral vocabulary a conversation is read as — text,
messages, reasoning, a tool-call lifecycle, a completed turn — and
`claude_code/projection.py` is the reducer from Claude's frames into it. Together they are the
one interpreter that replaced the four that each read Claude's frames independently.

**The two halves sit on different levels, which is the placement rule doing its job.** The
vocabulary names no backend and every surface renders it, so it stays at the runtime level; the
adapter cannot be written without knowing what `assistant`, `stream_event` and `tool_use_result`
are, so it lives under the harness directory beside `claude_code/frames.py`, which is where those
names are spelled. A second backend adds a sibling adapter and touches neither the vocabulary nor
its readers.

**The reducer's own contract is in `claude_code/projection.py`** — what `project` means, why a
batch boundary is not an ending, and the wire facts every rule in it answers. It is not restated
here; what follows is who reads it and what is not yet on it.

**The live turn loop is the only thing that folds frames.** A stored session is read from the log
instead: `SessionStore.read_transcript` selects the session's `conversation_event` rows and
`transcript_entries.py` folds them onto the read models in `conversation_records.py`, so
`haku_conversations` needs neither an adapter nor the session's `runtime_kind` to answer what was
said. There is no whole-log projection any more — no `project_log` on the adapter, and no `finish`
that declares a stream over — so an item the frames left open stays open, and only a frame closes
one. A capture is still folded in tests, through the same reducer one frame at a time
(`claude_code/testing/fold.py`).

**One typed integration boundary.** `_run_turn` gives each exact native frame to the selected
runtime's stateful `RuntimeTurnHandler` and acts only on its neutral `FrameEffects`. The integration
also owns prompt-submission recognition and completion/failure interpretation. Adoption drives the
same handler from the durable cursor, and reprojection drives it again to check the stored rows; no
generic layer reads a provider discriminator.

### The events, stored — `conversation_event`

`session_events.py` maps a `ConversationEvent` onto a row and `apply_frame` writes it inside the
transaction that moves the cursor, so a row exists exactly when the cursor says its frame was
projected. The same module maps the console's own facts about a session onto rows, written in the
transaction that makes each true, and the operator's prompt onto one written in `enqueue_prompt`'s.
What the table holds that nothing else does is a **tool call's answer**.

- **An item is found by `item_id`**, minted where the item opens and carried by every row about it.
  Nothing joins on a frame range and nothing on the agent's own id for a message, which a great many
  production rows do not have.
- **Provenance is a column, and `NOT NULL`.** `frame_range` carries both ends and `authored`
  carries neither, which the table's own constraint makes the only two possibilities.
- **The two kind enums split the stream by whether a row is about an item.** The three
  `ConversationEventKind`s are one item's lifecycle — started, any number of segments, completed —
  and `AuthoredEventKind` is what the console witnessed about the session or the turn instead. Which
  _provenance_ arm a row takes does not follow from its kind: an item may be folded from frames or
  authored, because a prompt is an item the console accepted before anything crossed a wire.
- **Prose exists only as segments, and a completion carries none.** That is what makes
  `conversation_item.text` a fold of these rows rather than a second authority for the same words —
  `reprojection` folds the segments back and reports a disagreement, which the shape this replaced
  could not be asked.
- **An abort is a turn's outcome, not an event of its own.** `end_turn` writes `turn_ended` with the
  outcome on it under the same lock that closes the turn, which is where every backend protocol puts
  it, and the room's "aborted" line is that row rendered.
- **One member of the vocabulary has no row.** A `TurnCompleted` is the `conversation_turn` row,
  which already holds the exchange's outcome and its two brackets.

**One status source the neutral vocabulary does not carry**, worth knowing before it is missed: the
prose the CLI writes on `system/task_started`, a background command's own command line. It is
Claude's concept keyed by Claude's identifiers, so `task_started` lands in `Projection.unprojected`
and a turn that would have shown it shows `writing` instead. The frames stay in `session_frames` for
a reader that wants the detail.

### The cursor — where the fold resumes, and what makes its effects exactly-once

`sessions.projected_frame_seq` is the `frame_seq` of the last frame whose projected effects are
committed, and `SessionStore.apply_frame` is the one transaction that writes both: the log rows, the
items they materialise and the cursor land together or not at all. Nothing is queued for a channel
there — the turn writes the log and stops, and each attached channel reads forward from its own
position and decides what it owes. So a replica
that dies leaves the session naming the last frame that took, and its replacement redoes exactly
the frames whose effects did not.

**So adoption is not a second reading of the log.** `adopt_open_turn` hands back the recorded frames
past the cursor and `handle_runner` feeds them to the turn loop ahead of the live stream
(`_replaying`), so a turn's remaining frames go through the same call whether they have just arrived
or arrived at a replica that is gone. A turn whose ending is among them closes without the socket
being consulted.

Four things to know before changing it:

- **One provider-owned handler folds the whole turn.** `RuntimeAdapter.turn_handler` owns typed
  native protocol state and returns neutral `FrameEffects`; the generic loop never reads native
  discriminators. Adoption seeds that handler only with durable neutral facts (the open message
  and seen call ids), then replays every raw frame after the cursor. A handler returns
  `Checkpoint.HOLD` while private state such as partial tool JSON cannot be represented durably, so
  the cursor stays before the composition and a replacement rebuilds it from the same frames.
- **`next_prompt` anchors the cursor** at the frame before the turn it opens, in that same
  transaction. That is what lets adoption tell a position inside this turn from one left behind by
  a writer that does not advance it — a replica on the previous image, for the length of a roll —
  and refuse the second (`projected_frame_seq < first_frame_seq - 1`).
- **`complete_frame` carries the cursor past the frame that ended the turn**, not `apply_frame`.
  Its terminal neutral effects, fallback answer close, outcome and cursor advance are one
  transaction, so a provider may put durable facts on its terminal frame without losing them or
  letting the cursor outrun the turn close.
- **A turn is resumed from its cursor or not at all.** A cursor from before the turn — or none at
  all — is a position re-projecting from would redo effects that did commit, so `adopt_open_turn`
  ends such a turn as failed rather than resuming it. No session that can still acquire a frame is
  in that state.

**`read_transcript` never touches this cursor, or any frame.** It folds the session's log rows from
the log's start on every page — the transcript cursor is a position in that fold, not a durable
writer position — so a projection change reaches sessions still to run and leaves the ones that
already happened saying what was recorded of them.

Four properties hold the design up, each stated where it is kept — break one and the rest stop
meaning anything:

- `project` is pure and deterministic (`projection.py`).
- One batch and any split of batches project alike (`claude_code/test_projection.py`).
- Every event carries provenance, and it is a union, so a rebuild cannot delete an authored event
  (`conversation_events.Authored`).
- The default branch is counted, not dropped (`Projection.unprojected`). No surface carries that
  count any more — `read_transcript`'s `unreadable` is the same warning about the log rather than
  the wire — so what asserts it is the adapters' own capture tests, which name every frame class
  a recorded session reached the default branch with. The raw-frame inspector itself presents exact
  JSON and does not classify native frames.

Before changing the adapter, read `../../cli_protocol/protocol.md` and the adjacent fixtures:
the wire is version-pinned, and tests preserve the shapes the projection must tolerate.

### Re-projecting a stored session — `reprojection.py`

`check_session` re-projects a recorded session's frames and aligns the result against
`conversation_event`, returning per turn either `Agrees`, `Drifted` with its findings, or `Skipped`
with the era it cannot speak about — and, separately, every item whose `text` is not what its own
segments concatenate to. It is a function rather than a command, and has no caller in the
tree.

- **It folds through the selected integration's `RuntimeTurnHandler`**, the same stateful reducer
  the live turn loop drives. That keeps partial native composition, deduplication and terminal-frame
  effects identical between the writer and the checker.
- **It aligns by frame.** One frame's rows are written in one transaction and each event's range is
  `(frame_seq, frame_seq)`, so which rows a frame owns is a lookup. It reads a turn's rows on the
  `frame_range` arm only, so the authored category is out of scope — which is what stops a rebuild
  from deleting what it cannot re-derive. That filter is load-bearing now that `turn_aborted` names
  a turn.
- **One era bounds what it may speak about**, per turn and named by `SkipReason`: a turn whose
  frames the cursor never reached (#4178's era). A turn with frames and no rows at all is not that
  — nothing writes a projected frame without its rows — so it is drift, and is reported as drift.

### Recording a Claude session as a fixture — `claude_code/frame_export.py`

`claude_code/testdata/diverse_session.jsonl` was captured off the CLI's stdout in a throwaway
directory, so it can only ever hold a session somebody ran for the purpose. The shapes still
unrecorded are the ones a console session produces — a `Task` subagent, a backgrounded `Bash`, the
`BashOutput` loop watching it — and those exist in `session_frames`. The Claude-owned exporter is the
second route to the same file format:

```bash
bb run //haku/console/x/claude_code:frame_export_bin -- \
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
- `sync.py` — logs in as `@haku`, long-polls `/sync`, binds the one room Haku services, and hands
  what the operator types to its attached conversation. Holds the only Matrix credential, so
  everything that speaks into the room speaks through it.
- `conversation.py` — the room's attachment to a conversation and ingress (`MatrixTurns`). It does
  not create, replace, resolve or tend runtime sessions.
- `pacer.py` — one paced outbound queue per room, over Synapse's `rc_message` budget.
- `outbox.py` — the room's outbox: replies as `matrix_outbox` rows, and the drain that says them.
- `revisions.py` — which homeserver event the channel is currently editing for a revisable subject
  (`matrix_revision`), which is what the status line is edited and retired through.
- `room_subscription.py` — the room as a subscriber: its durable position in the conversation
  (`channel_cursor`), the replies it queues from it, and the notices it says from it.
- `ingress_ledger.py` — which inbound events a prompt in the record carries
  (`matrix_ingress_event`).
- `formatted_body.py` — Haku's Markdown into the HTML subset Matrix clients render.

**The room reads the record; the turn loop never pushes at it.** `room_subscription.py` projects
answers, aborts, silence, setup narration, refusals, unreadable input, session handoffs and lease
losses, plus prompts arriving through another surface, from one cursor and in one order. Only facts
outside the conversation log — such as binding or adopting a room — are still announced directly.
The position is kept after the batch, so a crash costs a repeat rather than silence: a notice may be
said twice, while the outbox's unique subject refuses a duplicate reply.

**A prompt is a conversation fact, so every attached surface shows it.** The prompt item's origin
names the surface it arrived through, and the room compares it against its own address: a prompt
typed here is already in the timeline, and one from the SPA or a sibling room is posted, marked as
having come from elsewhere. It is said at the item's completion, where its whole text is readable.

**The outbox is the channel's own, and the channel is what writes it.** The turn writes the log and
addresses nobody; a completed message item is what the room's subscriber turns into a `matrix_outbox`
row, and the drain says it. That is the shape every outbound channel write has to take — decided
from the record, sent from a queue — so a second channel inherits the record and writes only its own
half.

Behaviours worth knowing before reading the code:

- **A produced reply is a row, not a call.** The subscriber writes it when it reads the message
  complete, and `RoomOutboxDrain` — one replica, under the `MXOB` advisory lock —
  claims the oldest, queues it into the pacer, and marks it `sent_at` only once `room_send` has
  returned. Everything else the console says —
  the status line, lifecycle and rejection notices, bootstrap narration — stays on the pacer's
  in-process queue, because a notice describing a moment is not worth redelivering ten minutes
  later. Two rules the drain is deliberate about: a failed reply **halts** the queue for its backoff
  rather than being overtaken, and the one row stepped over is one out of `MAX_SEND_ATTEMPTS`, kept
  unsent with its `last_error` and logged loudly rather than deleted.

- **What is written down is what the channel can still go back and edit**, and only that.
  `matrix_revision` pairs a subject with the room event it became, per attachment, and the status
  line is its one reader: the event id lives there rather than on the sync service, so the replica
  that adopts a session edits the line its predecessor posted instead of posting a second one beside
  it. There is deliberately **no row per delivered message** — a flushed-up-to position materialised
  one row at a time is what `channel_cursor` holds properly.

- **A rejected batch is not queued anywhere.** Conversation admission accepts with no session or on
  an idle/ready session, provided no turn is open and nothing is pending; a refusal is the answer: the room is told the messages were
  not delivered and what to wait for, and the watermark advances past them so the homeserver does
  not offer them again. **Admission is that one transaction's alone**: `MatrixTurns.offer` asks no
  status question of its own, because an answer read outside `enqueue_prompt`'s
  `SELECT … FOR UPDATE` could only agree with a decision that had not been made yet. It turns the
  refusal into a `PromptRejected` carrying the reason and the fact that records it.
- **The rejection is a row, written with the watermark.** `AuthoredEventKind.PROMPT_REJECTED`
  carries the reason and the text, and `MatrixSyncStore.advance` appends it in the transaction that
  acknowledges the batch — so a crash cannot acknowledge a message while losing both the record of
  it and the operator's only account of what happened. The room notice is a rendering of that row.
  What a refusal is about is the conversation, which exists from the moment the room is bound; the
  absence of a session is durable runtime demand rather than a Matrix refusal.
- **An accepted batch is acknowledged at once, and the prompt is the conversation's.** Nothing
  re-delivers a message the homeserver has been acknowledged for, so what protects the operator's
  question is that the queue outlives the session that took it: a sandbox dying before the turn
  strands nothing, because the replacement's own `next_prompt` finds the same row
  `channels/matrix/ingress_ledger.py` records the events a prompt
  carries in that prompt's own transaction, which is what makes a re-delivery recognisable.
- **An event Haku cannot read is announced, not held.** `m.text` and `m.emote` are prose and are
  serviced; an `m.image`, `m.file`, voice memo, or an msgtype invented after this release is
  carried out of the sync as an `UnmappableEvent`, said out loud in the room, and then
  acknowledged. Refusing the batch instead would never converge — nothing about an already-sent
  screenshot changes, so it would wedge ingress against every later message. It is recorded the
  same way a rejection is, one `AuthoredEventKind.UNREADABLE_INPUT` row per event.
  `m.notice` is neither serviced nor announced: it is the msgtype Haku's own status, lifecycle and
  unreadable-event lines go out under, and excluding it from any sender is the second of two
  independent guards — the first being the sender rule — against a notice about an event being
  itself an event.
- **One replica syncs.** The loop holds a Postgres advisory lock (`MXSY`) for its lifetime —
  `/sync` is a long poll, so releasing between passes would let two replicas double-process a batch.
  Conversation runtime supervision is a channel-neutral sibling under `CRUN`; it creates or
  replaces the one idle session durable prompt demand needs and performs global lease/claim
  maintenance. Sandbox allocation is another neutral sibling under `SBOX`. They are elected
  independently and can land on different replicas, so a stalled claim cannot wedge ingress or
  make Matrix the only surface capable of recovering durable demand.

## Tests run against a real database

Every store here is exercised through Postgres (the `migrated_*` testcontainer fixtures), never a
stand-in. What stays faked is what is genuinely outside: Kubernetes and the Agent SDK client. The
rule is not tidiness — a fake store answers from the shape the test author imagined, so it agrees
with whatever the code does:

- A fake `_listen` passed against a fake engine while the real one raised on **every** call
  in production, because it was written against psycopg3's API on an asyncpg engine.
- A fake conversation store let a test bind a room to a session id that had never existed.
  Postgres refuses — the binding is a foreign key — so the test was describing a scenario the
  schema forbids.

The one deliberate exception is `FailingEngine`, which exists to make a connection fail —
there is no way to ask a healthy Postgres for that.

### The runtime's conftest names no channel

The placement rule above applies to the fixtures too, and it is the one place it is easy to lose:
`conftest.py` is inherited downwards, so a runtime-level fixture that reaches into
`channels/matrix/` makes every runtime test depend on a homeserver's vocabulary and makes a second
channel unaddable without dragging Matrix along. So `x/conftest.py` holds the stores, the service,
the claim stand-in and the operator's identity — and nothing a room knows — while the homeserver
identities (`MATRIX_*`), the config they compose into, and the room/session binding
(`conversations`) live in `channels/matrix/conftest.py`. `OPERATOR_SUBJECT` is imported down rather
than restated, because `MATRIX_CONFIG.operator_subject` and the `operator_id` fixture have to name
the same operator.

The test files divide on the same seam. `test_session_runtime.py` and `test_session_store.py` use
only conversation rows and a `chat_attachment` address — never a channel port or `matrix-nio` — so
what they cover (the turn loop, the outbox row, provenance, adoption, the abort
drain, prompt fate) is what a second channel inherits. A message _arriving_ from a room and becoming
a turn is the crossing itself, so `MatrixTurns.offer` is tested in
`channels/matrix/test_conversation.py`, beside the module that defines it.

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

What a test drives them _through_ lives there too, so a second e2e inherits it rather than copying
it: `channels/matrix/testing/operator_room.py` is the operator's side of a room — an
`nio.AsyncClient` of its own, handing back typed `RoomEvent`s rather than Matrix JSON —
`channels/matrix/testing/console_deployment.py` is the console replicas serving that room, and
`testing/waiting.py` is the bounded poll under both, at the runtime level because a bounded poll is
nobody's channel. It shares nio with `channels/matrix/client.py` and deliberately **nothing else**:
nio is third party and is not the subject, but `EventTag`, `SyncResult` and the event mapping on top
of it are exactly what these tests are checking, so reading the room back through them would check
them against themselves.

The stub `claude` is a `py_binary` for one further reason: the runner execs `HAKU_CLAUDE_PATH`
directly, and a source file staged in runfiles does not reliably carry its executable bit. One stub
serves both tests, parameterised by the directions a prompt carries (`[hold]`, `[narrate=N]`,
`[silent]`) and by `HAKU_STUB_GREETING`.

### The homeserver is a real Synapse as well

`channels/matrix/test_homeserver_e2e.py` brings one up in a container
(`channels/matrix/testing/synapse_container.py`) and runs `MatrixClient` against it, with the room's
other side driven straight through the client-server API rather than through the client under test.
It exists for the questions a canned response can only agree with: whether a resumed `/sync` across
a gap larger than `TIMELINE_LIMIT` really comes back truncated and really paginates back to every
missed message once (downtime recovery — the reason the target exists), whether a sync watermark
really is a `/messages` token, whether a repeated
transaction id really is refused, and whether the `works.allegedly.haku` tag survives the wire on
both halves of an edit.

`channels/matrix/test_client.py` keeps its canned homeserver, and should. The split is which side of
the wire the question is about: what Synapse does belongs in the e2e target, what the parser does
with what Synapse said belongs in the unit tests — where an unknown tag kind or an exhausted
backfill budget costs one dict rather than thousands of messages.

### And the whole surface, as processes

`channels/matrix/test_fullstack_e2e.py` composes those two targets with the bridge one: that
Synapse, a console replica as its own process (`channels/matrix/testing/console_replica.py`, with
the real sync loop and supervisor), a runner process per sandbox behind a stub `claude`
(`claude_code/testing/stub_claude.py`), and a real Postgres. It asserts one operator-facing property
— every message the operator sent has exactly one reply in the final room — which is what nothing
below the whole stack can answer.

Three of its tests take that property through the ways a produced reply can be lost: a refused send,
a reply still queued when the replica stops, and a console roll across the gap between recording an
answer and saying it. `channels/matrix/outbox.py` is what closes all three, and the fourth is the
quiet-path control.

The console is a process, and the sandboxes are started by the test off the claim files that console
writes, for one reason: a sandbox has to outlive the console for there to be an adoption at all.

## `session_notifications.py` — the wake channel

`LISTEN`/`NOTIFY` for the chat surfaces, deliberately **not** part of `SessionStore`: a
repository answers questions about rows, and this wakes tasks. Merging the two is what let
the listener be written against psycopg3's API while running on an asyncpg engine.

**One channel, `session_events`, carrying a `SessionEvent`** — `{kind, session_id}`, where `kind` is
`prompt`, `update`, or `abort`; waiters register on `(kind, session_id)`. `console_events.py` stays a
separate channel and a separate connection: it is a different subsystem with a different payload and
its own lifecycle, and the only thing the two share is the mechanism.

**Waiters name a session; watchers cannot.** `wait`/`subscribe` register on `(kind, session_id)`,
which is what the turn loop and the supervisor want. `watch` is the other shape — every event of a
kind, whatever session it names — and exists for `session_live_updates.py`, which has to hear about
sessions nothing has told it to expect. Its gotcha is the mirror of the waiters' one: a reconnect
can be replayed to a waiter (`_wake_everyone` tells it to re-read the session it already knows
about) and cannot be replayed to a watcher, so a watcher must be something a missed event only
delays.

`test_notify_puts_a_readable_event_on_the_channel` pins the wire format — channel name and
envelope, read off a raw connection. Nothing else would notice if either drifted, because every
other test has the same code on both ends.

One long-lived connection with a reconnect loop, matching <../console_events.py>, the console's
other LISTEN consumer. The notify half stays inside the caller's transaction, because `pg_notify`
delivers on commit.

**Gotcha for anyone changing the channel name or payload:** the Deployment rolls with
`maxUnavailable: 0`, so old and new replicas run together for the length of a roll. A renamed
channel means the new replica notifies where the old one is not listening, and the wakes are lost
for that window — the same expand/contract discipline a destructive migration needs. Notify on both
names for one release, then drop the old, and gate that second release on the roll having
**converged** (every pod on an image at or after the first) rather than on a release having elapsed,
since `maxUnavailable: 0` means a bad image stalls the roll with the old replica still serving.

The trap in the overlap phase: while both names are being notified, every wake is delivered twice,
so a woken waiter proves nothing about which name woke it. Tests and production alike will look
healthy with the new path entirely broken, right up until the old one is deleted. Cover the new path
end to end on its own before contracting — a test driving `pg_notify` on exactly one channel.

## `session_live_updates.py` — telling open tabs, over the socket they already hold

The console's own live channel (<../console_events.py>) reaches every tab the operator has open,
and this is what puts session changes on it: a `SessionChangedEvent` naming the session whose rows
moved. **An invalidation, not a payload** — the surfaces reading it hold a list and refetch it, so
a tab that missed events lands correct by reading again and no consumer has to decide whether the
socket or the API is the truth. A surface that holds a transcript and a position follows the
conversation instead (below); this is for the ones that hold neither.

What it is deliberate about:

- **Nothing publishes anything new.** Every write that changes a session already notifies `UPDATE`
  inside its own transaction, so the announcement belongs to the commit rather than to a sweep.
- **No second `NOTIFY`.** `LISTEN` is broadcast, so each replica already hears every session
  event and turns what it hears into sends on the console sockets **it** holds
  (`ConsoleEventHub.deliver_locally`). Relaying through `broadcast` would notify twice for one
  change and deliver it to every tab twice.
- **Coalescing is the point, not tidiness.** `UPDATE` fires per stream delta, and each event costs
  every listening tab a whole list read — far more than the notification that triggered it. One
  event per session per `COALESCE_WINDOW` (500ms) is what keeps the invalidation cheaper than the
  read it triggers.

Routing costs a lookup: `SessionEvent` carries no operator and the hub delivers per operator, so
the session's owner is resolved once per session and kept (a session's owner never changes).

## `subscription.py` — reading a conversation from a position

The wake above says _which_ session moved; this is what a woken consumer reads, and from where.
A conversation is an ordered stream of `conversation_event` rows addressed by `event_seq`, dense
within the conversation, and a **subscription** is one consumer reading it from a position.
`ConversationStream.read` is the shared half — everything after N, keyed by the thread rather than
the session, so a position survives a session being replaced. Density is what makes a position an
answer rather than a hint: a subscriber can tell a gap from an end.

**The position is the subscriber's, and where it lives follows from what the subscriber holds**
(operator, 2026-08-17). That is the whole reason `Cursor` is the only port here:

- **A tab holds no copy that outlives it.** Several tabs can watch one conversation at different
  points, and persisting any of those would store rows for something a refresh destroys. So its
  position is the request's own argument (`ClientHeldCursor`) and the console keeps nothing.
- **A room holds a federated copy that outlives every console process.** After a restart the
  channel has to know what it already put there, so its position is durable — in `channel_cursor`,
  keyed by the attachment, which is the one piece of channel state the conversation layer keeps
  generic: a position in the log is the resume contract every attached channel owes it, and the
  same integer answers it for all of them.

**There is deliberately no shared cursor table**, because durability is one implementation's
concern rather than a property of subscribing.

Three consequences worth knowing:

- **Read, then keep.** `Subscription.read` never advances. A durable subscriber keeps its position
  once it has done what the events oblige it to, so a crash in that window replays rather than
  skips — the discipline `delivery_log.retire` already follows.
- **An absent position means _never read_, not _at the start_.** Only a kept position can be absent
  (`Unstarted`), and what a subscriber does about it is its own decision: the room takes the head
  silently, because a room bound before it kept a position already shows everything said in it.
- **A gap is not a loss.** `event_seq` is global, so one conversation's rows are not contiguous.
  Every read is "everything after N", which makes a hole undetectable by construction rather than
  something to notice.

The one consumer today is the Matrix room's notices (below). `conversation_follow.py` reads a
conversation the same way and keeps nothing, so it holds a follower's position as a plain integer
off the socket rather than through a `Cursor`; `ClientHeldCursor` is that case spelled as the port.

## `conversation_follow.py` — following a conversation, as one operation

A follower names a conversation and is sent its state, then the changes to it:
`WS /api/conversations/{conversation_id}/follow`, optionally carrying `?after=N`. **The snapshot
and the updates are one call**, so initial load and reconnect are the same code path on both ends
and there is nothing for a caller to combine.

- **A snapshot replaces, an update merges.** Every message names the `event_seq` it leaves the
  follower at; updates carry whole rows keyed on `message_id`, so a duplicate costs nothing,
  delivery need not be exactly-once or ordered, and re-reading from an older position is always
  correct.
- **The ordering is inside the operation.** Wakes are registered before the state is read, so a
  change landing between the two cannot be lost — and it costs a flag rather than a buffer, because
  the wake carries no payload and the read that follows it is positional.
- **A position that cannot be served is answered with the conversation whole**, never an error: a
  log that no longer holds the position and an update that would carry most of a snapshot recover
  the same way, so a client has no repair path to get wrong.
- **What crosses replicas is still an id.** `pg_notify` carries `{kind, session_id}`; the replica
  holding the socket reads the rows itself. Nothing about a payload rides the notification, which
  is what keeps the 8000-byte cap and the expand/contract discipline out of this.
- **Coalescing bounds the open message.** `content` is rewritten in place as prose arrives and a
  `TextDelta` is not a row, so every update re-sends the message being written, whole; without a
  window that is bytes quadratic in a turn's answer. 500ms, which also sets how fast prose appears.
- **The transcript is incremental and everything else is whole.** Messages and turns are the only
  part that grows without bound, so they are the only part worth addressing by position; the
  attachments, the earlier sessions and the live session's own row go out every time. A field
  carried only in the snapshot would be one a tab can never be told has changed — which obliges
  the writers in turn: **anything that changes what a conversation shows must notify `UPDATE`**,
  because this loop reads when it is woken and at no other time. `SessionStore.narrate` is the
  example to copy.
- **The sandbox is polled, not awaited.** What Kubernetes says about a claim, a pod and a runner is
  an observation of another system, and no `conversation_event` row is written when a pod goes ready —
  so there is no wake to carry it. While the session being followed is still coming up, the loop
  re-reads on `SANDBOX_POLL` as well as on wakes, at the rate `OBSERVATION_TTL` already bounds
  cluster reads to; a session past provisioning is not polled at all.
- **The connection is the subscription.** One socket per followed conversation — the position and
  the reader task are all the per-connection state there is, and a send-only socket has no inbound
  protocol that could be talked into reading another operator's thread.
- **The browser's types for these messages are generated from these models.** A WebSocket has no
  route for FastAPI to document, so `export_schema.py` publishes `ConversationFollowMessage` into
  the OpenAPI document the SPA is generated from; the messages reuse the components a conversation
  read already defines rather than describing those rows a second time. Renaming a field here is a
  compile error in `frontend/x/conversation_follow.ts`, not a message a tab cannot read.

Addressed by the conversation and never by the session: a session exists only while it holds a
sandbox, so a follower naming one would be reading a dead log after every replacement. The rows an
update carries are whichever sessions moved, and `session_id` says which one holds the thread now.

## Cross-replica state, and the trap it sets

`replicas: 2` means any given HTTP request reaches an arbitrary pod, while a session's live
objects — the runner's bridge websocket, its `ClaudeSDKClient`, its abort event — belong to
exactly one. **Anything that has to reach a running turn therefore goes through Postgres `NOTIFY`,
never an in-process registry**: a dict keyed by session id looks correct in tests and
single-replica dev, and silently answers "no such session" in production about half the time. That
is what the `abort` event is for.

## What necessarily lives outside this directory

The stable modules own these, so moving them here is not possible without inverting the
dependency:

- `MatrixConfig` and `Settings.matrix` in <../config.py>. Absent config, or a config whose
  reflected bot password has not landed yet, means the surface does not start and the console
  does.
- `Session`, `Conversation`, `ChatAttachment`, `MatrixAccessToken` and `MatrixSyncWatermark` in
  <../database_schema.py>, plus their Alembic revisions — migrations are
  one lineage for the whole database.
- `StatusFrontend` is declared beside the stream fold in `room_status.py`; Matrix's sync service
  implements those ephemeral operations. The turn runtime has no channel port.

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_layers.md> — what a session, a conversation and a channel each own, the two edges
  between them, and where a new table, port or event kind goes. Read it before adding one.
- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../plans/conversation_layers.md> — what is still wrong and the order to fix it, and
  <channels/matrix/SPEC.md> — what the Matrix channel already guarantees.
- Incident narratives and superseded implementation notes are not maintained beside this
  experimental surface; Git history is the record when their detail is needed.
