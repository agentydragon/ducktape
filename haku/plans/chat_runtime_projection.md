# One state machine instead of two

The chat runtime handles a lot of corner cases, and they nearly all sit in one place. This is what
that place is and how to remove it. It supersedes nothing in
<chat_runtime_cleanup.md> — that plan's remaining stages are about behaviour, this one is about
where the behaviour's state lives.

## The problem, stated once

`_run_turn` holds a turn's state in **local variables** — `assistant_id`, `streamed`, `spoke`,
`saw_assistant_message`, `result`. Everything durable is a side effect of them.

So when the process holding those locals dies, a second body of code exists to reconstruct them
from the log: `adopt_open_turn` and its helpers `_prompt_left`, `_recorded_result`,
`_said_anything`, `_streaming_assistant`, `_requeue`. That is the same state machine written
twice, and every corner case the runtime has needed is at the seam where the two disagree:

| Symptom                                            | Which disagreement                                                                    |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- |
| A claimed prompt nobody ever asked                 | The live path claims before writing; recovery has to guess whether the write happened |
| A turn whose `result` is logged but never closed   | Recovery has to detect a death between two writes                                     |
| `spoke` cannot tell delivered from merely recorded | Recovery cannot rebuild a local that was never durable                                |
| `TurnResumed.streamed`                             | Recovery re-seeding a local by hand                                                   |

Roughly fifty comment sites in `claude_chat.py` reason about "already / twice / replay /
duplicate". That is the tax.

## The shape

Two stages with a **durable cursor** between them.

**1. Transport ↔ log.** The socket's only job: CLI frames into `session_frames`, deduplicated
by `frame_uid`; queued prompts out to the CLI. It knows nothing about turns, messages or rooms, so
it can die anywhere without leaving a decision half-made.

**2. Log ↔ world.** A fold, `project(state, frame) -> (state, effects)`, reading from a per-session
cursor. Effects are rows: message upserts, turn open and close, room-outbox entries. **The cursor
advances in the same transaction as its effects**, which is what makes them exactly-once.

The payoff is not the two loops. It is that **live and recovery become the same code path**:
steady state is "project each frame as it lands", adoption is "project from the stored cursor,
which happens to be behind". There is no second implementation left to disagree with the first.
It costs no latency either — the fold runs inline in the happy path. Two loops logically, one call
stack in practice.

What that deletes: `TurnResumed`, `adopt_open_turn`'s three-way case analysis, `_recorded_result`,
`_said_anything`, `_streaming_assistant`, `_prompt_left`, `saw_assistant_message`. `spoke` stops
being a guess and becomes "the cursor passed this frame".

Room delivery becomes an outbox drained by `matrix_pacer`, which is already a queue and needs only
to become durable. Then `spoke` is "the outbox row is marked sent", `EventTag.transaction_id` is
uniformly the outbox row id with no derive-versus-mint rule left, and `_deliver_reply`'s
"deliberately not fatal, TODO retry" resolves itself — retrying is what an outbox does.

## What this is not

**Not a large line-count win.** Estimate: −350 from the reconstruction cluster and the partial-frame
machinery, +150 for the fold and the outbox, against a 2400-line file. The win is that the number of
things which can disagree goes from two to one.

**Not a big-bang rewrite.** It is the hot path in production, and the stages below are ordered so
each is independently useful and reversible.

## Stages

Stage 1 — a log with no hole for the fold to trip over — is the foundation the rest needs and is in
place: every frame class is recorded, deltas included, where before the delta gap was precisely what
forced `streamed` to be carried by hand. One thing it leaves owed: **measure the delta cost** against
a real session's `count(*) where kind = 'stream_event'` before stage 2 commits to a row per delta. A
long turn streams a few hundred, tens of kilobytes each; `read_frames` already keeps them out of its
default view, and the extra write per delta is a wash once stage 2 removes the `partial` row it
replaces.

### 2. Make the frame log store one thing — recorded, **not scheduled**

`session_frames.kind` holds **two different discriminator vocabularies**, because two unrelated
sinks write to it:

| Writer                                                  | Sees                                      | Writes into `kind`                             |
| ------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------- |
| `RolloutRecorder._record`, a `FrameSink` on `ClaudeCli` | CLI protocol frames only, by construction | `payload["type"]` — the CLI's vocabulary       |
| `_progress_reporter.report`, by hand                    | one decoded line of a `SetupOutput`       | `"setup_output"` — the bridge's `kind` literal |

Plus a `partial` row, the console's own reconstruction of a streaming answer, which wears
`assistant` and is told apart by a boolean column. That one leaves regardless: it is tombstoned on
`update_partial_frame`, and stage 1 removed its reason to exist.

**The intended shape, if and when this is picked up: the table is the log of the bridge.** Nothing
here is scheduled, and the rest of this plan does not depend on it — stage 3 onward can proceed with
the schema exactly as it is. `kind` becomes the envelope discriminator
(`claude`, `setup_output`, `hello`, `start`, `end_input` — `protocol.py` owns the list) and the
CLI's own `type` gets a column of its own. Two columns, each answering one question, and any
future runner-originated frame has a home instead of a special case.

**What this costs, stated before starting.** The recorder is a `FrameSink` on `ClaudeCli`, which
sits _above_ the envelope and structurally cannot see one — so the sink moves down to
`WebSocketTransport`. The dedup answer moves with it: `received()` returns False for a replay and
`ClaudeCli._read` uses that to skip routing, so the transport takes over both. It also means
recording envelope kinds nothing reads today (`hello`, `start`, `end_input`); that is the point of
the design rather than a cost, but it is new rows.

**No dedup win to claim.** `SetupOutput` is queued `replayable=False`, and `prepare_workspace`
sends it straight to the socket before launch anyway — so it is never replayed and there is no
duplicate-row bug here for A to fix.

Three releases, because flipping a column's meaning under a rolling deploy is not additive:

1. **Add `cli_type`, dual-write, and move every reader onto `coalesce(cli_type, kind)`.** Backfill
   `cli_type = kind` where `kind <> 'setup_output'`. Additive; old replicas are unaffected.
2. **Once that roll converges**: writers set `kind` to the envelope discriminator, and the sink
   moves to the transport. Safe only because step 1 already stopped every reader depending on
   `kind` — an old replica still filtering `kind = 'assistant'` would otherwise mis-decide a turn's
   state in `adopt_open_turn`, which is not cosmetic.
3. **Contract**: drop the coalesce, `kind` NOT NULL. `FrameKind` splits into the closed
   `BridgeFrameKind` (which _can_ be the column's type, since `protocol.py` owns that vocabulary)
   and an open CLI type that stays text, because the CLI may send one we have never heard of.

**Done when** `kind` answers one question and `BridgeFrameKind` is the column's type.

### 3. Turn state onto the turn row

The cheapest step with most of the benefit, and the one to do next. One additive migration; the
loop keeps its current structure and reads and writes its state instead of holding it. This alone
kills `TurnResumed`, `_said_anything`, `_streaming_assistant` and the `spoke` guess.

### 4. The fold, and what it projects **into**

`_run_turn`'s frame `match` becomes `project`, with the cursor advanced beside its effects. The
abort path collapses here too: an abort becomes an intent the transport writes, and the CLI's
answer comes back as frames — which is what removes the "exactly one `anext` in flight" dance.

An earlier draft called this "mostly moving code once stage 3 has made the state durable". That
undersold it. The fold is not a relocation of the `match`; it is where the system decides **what a
message is**, and that decision is currently made four times in four places.

#### The four interpreters, counted

Frames are authoritative: they are what the runner protocol plus the inner CLI's protocol put on the
wire. Everything else is derived — but nothing says so, so four bodies of code read Claude's frame
vocabulary independently:

| Where                             | Reads                                 | Produces                                        |
| --------------------------------- | ------------------------------------- | ----------------------------------------------- |
| `_run_turn`'s `match`             | `assistant`, `stream_event`, `result` | message rows, live                              |
| `adopt_open_turn` and its helpers | the same, from the log                | the same, reconstructed                         |
| `session_views.rollout_calls`     | `assistant`, `user`                   | tool calls **and their results**, on every read |
| `room_status.coarse_status`       | `assistant`, `system`/`task_started`  | the room's status line                          |

The third exists because `session_messages.tool_uses` is a **half copy** — the calls without their
answers, because the turn loop drops the `tool_result` blocks — so the read path re-derives both and
prefers its own answer wherever the row can point into the log. The fourth means a backend whose
frames are spelled differently makes the room go silent while the agent works, and it is why the SPA
has no in-progress display at all.

#### A message is provider-neutral, and tool calls are in it

The alternative — messages as prose only, tool activity left in frames — was considered and
rejected, on an argument from what the channels already do (operator, 2026-08-16). **Matrix shows
tool calls in progress.** If that display is to come from a neutral source rather than from one
CLI's frame types, then tool calls have to be in the neutral layer, and as a **lifecycle** rather
than as completed records stapled to a finished message:

- `TextDelta`, `MessageCompleted` — what was said.
- `ToolCallStarted(call_id, name, input)` → `ToolCallCompleted(call_id, result, is_error)`.
- `Activity` — the harness's own prose for a step in flight (`task_started`'s `description`), which
  is the case that has no tool name at all.
- `Reasoning` — the agent thought, with a summary where it gave one. A distinct state rather than
  empty text: Claude emits `thinking` blocks and Codex emits reasoning summaries, and a thinking-only
  message currently renders blank, which is a live bug rather than a hypothetical.
- `TurnCompleted` — with a **neutral** usage shape. Today `end_turn` stores Claude's `result` payload
  as it arrived; leave that and "turn completed" quietly means "whatever that one CLI sent".

This is not a second copy of the wire. It is a normalization, and the difference is that a copy
duplicates a shape while a projection **replaces** one — the harness's content-block encodings,
thinking signatures, stream-event mechanics and `msg_…` id formats stay in frames as evidence, and
none of them appear in a message.

**Identity is ours.** `rollout_calls` joins on `agent_message_id`, the agent's own id — and a
production count found 1,417 assistant rows that have none, which render their tool calls with no
results and say nothing about it. The neutral message owns `message_id`; the agent's id is optional
provenance. This is the lesson `EventTag.transaction_id()` taught in stage 5: identity derived from
the wire fails exactly when the wire does not carry it.

#### The projection is not a one-way door

**From the SPA, an operator must be able to click a message or an event and read the actual
provider-specific frame JSON behind it** (operator, 2026-08-16). A normalization that cannot be
appealed is a normalization nobody can debug — and the whole reason for keeping frames authoritative
is that they are the record the projection can be checked against.

So every projected thing carries its provenance: the frames it came from, addressed by
`frame_seq`, not by the agent's own ids. That is #4105 (`session_messages`' inclusive frame range),
which this makes a **product requirement** rather than the diagnostic convenience it was proposed as
— and it should extend to tool calls and activity, not stop at messages, since those are exactly the
elements whose neutral form loses the most detail.

Two things fall out of the same pointer. The transcript's join to tool activity becomes a range
lookup rather than a scan-and-match on `agent_message_id`, which is what lets `rollout_calls` retire.
And the reprojection check below has a per-row subject: not just "do the rows match" but "does _this_
row match what _those_ frames project to".

#### What makes it safe: the projection is a pure function, and that is testable

The property that prevents drift is not "do not duplicate" but **determinism**. If `project` is a
pure function of a frame sequence, then:

- drift is **detectable** — re-project a recorded session's frames and compare against its stored
  rows, over real sessions, in CI;
- a projection bug is **repairable** — fix the fold, re-project, and the transcript is corrected,
  rather than a row written wrong staying wrong forever;
- and the rows with no agent id stop being permanently degraded, because the rebuild does not depend
  on the agent having supplied one.

Stage 5's outbox already relies on the fold being single-writer per session (see the closing note);
reprojection is the other half of that bargain and wants writing at the same time.

#### Pressure-tested against the two things that would break it

Neither is implemented; both are read from documentation rather than measured, in the same spirit as
<../runtime/x/claude_bridge/docs/second_backend.md>.

**A Codex backend.** Forces the four points above: identity cannot be borrowed; reasoning is a state
both harnesses have; `TurnCompleted` needs its own usage shape; and the status line has to derive
from neutral events or the room goes quiet. What it does **not** force is an approval concept —
approval requests and responses travel over MCP to the console's queue, not over this channel
(operator, 2026-08-16), and a harness that wants to ask about commands is configured not to in its
launch spec, which is what `CliBackend.resolve` is already for.

**A Telegram channel.** Breaks something different and sharper: **`sendMessage` has no idempotency
key.** The outbox's retry is safe against Matrix because a redrive reuses the transaction id and the
homeserver refuses it; against Telegram an ambiguous timeout genuinely double-posts. So a channel
port must **declare** whether it has an idempotency key, and R11.6's "possibly duplicated" marking —
deliberately not implemented in stage 5 because a stable transaction id left it no case to fire on —
is exactly what a channel without one brings back. Telegram also caps a message at 4096 characters,
so one neutral message can be several channel messages: "sent" is a property of a _(message,
channel)_ pair that may hold more than one remote id.

What survives both, unchanged: frames as per-backend evidence, the outbox as rows with a cursor, and
content as neutral markup rendered per channel — `matrix_markdown.py` already does the second half of
that, so the channels share a source and not a rendering.

#### Does a turn live over frames or over neutral events?

Over the neutral events (operator's question, 2026-08-16) — but the question is worth answering
carefully, because today the two are indistinguishable and the code picks the wrong one.

`session_turns` opens when a prompt is claimed and closes on Claude's `result` frame, whose payload
is stored as-is for cost, usage and duration. So the boundary is currently **one CLI invocation**.
Every consumer, though, is neutral: the room's typing indicator and status line bracket a turn, the
SPA renders turn boundaries inline in the transcript, the outbox keys a turn's last word by
`turn_id`, adoption asks whether a turn is open, and I3 wants a batch acknowledged when its turn
completes. None of them care how a particular CLI spells the end.

**Two concepts coincide today and need not always.** The _harness cycle_ — one prompt in, one result
out, carrying that invocation's usage — and the _conversational exchange_, which is what a person
means by a turn and what every surface renders. They are the same thing only because one batch makes
one prompt which makes one invocation. Three plausible changes separate them: mid-turn steering
(folding a message into a running exchange, deferred but planned), a backend needing several
invocations for one exchange (continuation, compaction, an internal retry), and a harness reporting
usage per invocation across an exchange that spans more than one.

So the turn is neutral, its boundaries are **produced by the backend adapter** (`TurnCompleted`), and
if the two ever diverge the neutral turn is the conversational one while "which invocations made it"
becomes frame-level detail — reachable through the same `frame_seq` range as everything else. Two
consequences: the neutral usage shape should be **aggregatable** rather than one payload, since an
exchange may sum several invocations; and `end_turn` storing the raw `result` stops being "what the
turn cost" and becomes evidence, with the cost living in columns that mean the same thing on every
backend.

#### Two decisions this leaves open

- **The SPA renders messages by default and must let an operator inspect the tool calls underneath**
  (operator, 2026-08-16). That is disclosure over one neutral source, not a second query path — but
  which surface shows what, and whether in-progress calls appear in the SPA the way they do in the
  room, is unbuilt.
- **Reasoning and tool activity are not rendered in the channels or the SPA conversation view for
  now** (operator, 2026-08-16). They are projected and stored; showing them is a later decision. Note
  what that implies: the neutral layer carries strictly more than any surface currently displays,
  which is the right direction — a projection that only holds what today's UI renders would have to
  be re-derived the moment a surface grows.
- **Durable tool inputs and results widen the read surface.** Commands, file contents and diffs
  become neutral rows reachable through the `haku_conversations` tools.
  <information*trust_tiers.md> reasons about who may read past \_conversations*; it will have to
  reason about who may read past _tool activity_. The index should keep embedding prose only —
  tool JSON would pollute the vectors — but that is a selection choice, not a boundary.

### 5. The room outbox — **done**

`session_outbox` holds each produced reply until the homeserver has taken it;
`matrix_outbox.RoomOutboxDrain` says it, under an advisory lock, through the pacer, marking it
sent only after `room_send` returns. `matrix_pacer` kept its deque and its budget: it is still
what decides _when_, and the console's narration still lives on it. What follows is what the
stage was written against, kept because the reasoning is still the design's.

**One correction to make before reading further.** "The transaction id is already there" below is
half true, and the half that is false is what a redrive would have broken.
`EventTag.transaction_id()` derives from the transcript row _only when there is one_ and mints a
fresh `uuid4()` otherwise — so a redelivery of a turn's abort notice, or of text that arrived only
on a `result` frame, would have posted a second message rather than being refused. Every outbox
row is therefore sent under **its own id**, which is stable for exactly as long as redelivery can
happen, and two partial unique indexes (on `message_id` and on `turn_id`) stop a second row being
created for one logical reply.

**Not done, and deliberately deferred: R11.6's "mark a late reply as possibly duplicated".** With
a stable transaction id inside Synapse's 30-to-60 minute dedup window, a late redelivery is
refused rather than duplicated, so the marking has no case left to fire on for anything the outbox
sends; past that window it would, and the retry budget is sized to stay inside it. Revisit if the
budget grows or if a channel without transaction dedup is added.

The drop audit (<../console/debug/message_drops.md>) turned this from the last stage into the one
with a live bug behind it, and gave it three constraints it did not have when this was written.

**Why the layer above cannot be made honest without it.** `MatrixSyncService.reply` appends a
closure to an in-process deque and returns, so `deliver` reports success before any HTTP request
exists. Whatever the turn loop records at that moment is a statement about a `deque.append`. The
`spoke` fix that landed makes the turn believe the surface rather than itself, which closes the
failures the surface can see; every failure the pacer meets afterwards — a 429 past two retries, a
5xx, a `login` the token closure needed — is still popped and discarded with a log line and no
retry, ever.

**The generalization to aim at, from <../console/plans/session_channels.md> § 1: a channel is
reconciled against the session rather than sent to.** The outbox is the push half — deliver
promptly; a per-attachment cursor on `chat_attachment` is the convergence half — deliver
eventually and at most once, with repair as the normal path rather than a retry bolted on. Two
consumers want the pair. Once lifecycle events are recorded rather than only announced, "the
room's queue" becomes "bring each attached channel up to this session's record". And relaying an
operator's console-sent message into the room stops being a feature at all: it is a transcript row
the room lacks, which is the same divergence as an undelivered reply, so the loop already closes
it. Build the cursor with the rows.

**A third consumer changes this stage's priority from tidy-up to prerequisite.**
<information_trust_tiers.md> puts a classifier in front of the drain, so that outgoing messages
are checked before they reach a room rather than after they have federated — and a classifier
needs somewhere durable to hold a message while it decides. An in-process deque cannot be that.
The queue is also what makes the check asynchronous with respect to the agent while staying
strictly before the room, which is the property that made the design worth having; the ordered
drain is what makes "the first failed message halts" a rule rather than a race.

**Write the row where the reply is produced, not where it is delivered** — in the same transaction
as `update_assistant`. At delivery time it does not survive a turn that raises between producing
text and speaking it, which is a real path today (audit E4) and would quietly stay open.

**The transaction id is already there.** `EventTag.transaction_id()` makes redelivery idempotent
server-side, so a redrive sweep is safe to write on day one rather than needing a dedup design
first.

**What it does not close, so nobody expects it to.** An abort drain that discards the assistant
frame before delivery ever sees it (E3), and the ingress side, where a batch is acknowledged at
enqueue rather than at turn completion (I3) and a message for an unserviced room is dropped once
the watermark advances past it (I2). Each is its own fix, and the audit lists them with the
requirement each one violates. Two that were on this list have since had theirs: the unmappable
event is announced rather than dropped (I1, #4087) and an empty turn says so (E5, #4088).

## The one thing to keep in view

**The projector must be single-writer per session.** The lease gives that, and it is the reason none
of this needs the fold to be re-runnable. An expired lease now means unowned rather than dead, but
the property still holds: `authenticate_bridge` admits one holder at a time while a lease is valid,
and expiry only makes the row adoptable — it never lets two projectors write at once. A future change
to the lease's meaning should be checked against this assumption rather than around it.
