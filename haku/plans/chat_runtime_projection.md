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

The last two are closed: stage 3 below made that state durable, so `_said_anything`,
`_streaming_assistant` and `TurnResumed`'s state fields are gone. The first two are still asked of
the frames, and stage 4 is what retires them.

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
being a guess and becomes "the cursor passed this frame". Stage 3 has since taken the first, the
third, the fourth and the last of those, and made `spoke` a durable fact ahead of the cursor; what
is left on the list is what stage 4 removes.

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

### 3. Turn state onto the turn row — **done**

`session_turns` carries `assistant_message_id`, `said_anything` and `queued_reply` (migration
`0043`), each written in the same transaction as the effect it describes: the message pointer with
`begin_assistant`, the other two with the `update_assistant` that completes a message and inserts
the room's outbox row. `_run_turn` reads them through `SessionStore.turn_state` at the top of every
turn, so a turn this process opened and one it adopted enter the loop the same way. `TurnResumed`
became `ResumedTurn`, which carries only a `turn_id`; `_said_anything` and `_streaming_assistant`
are gone.

`spoke` is now `queued_reply`: **an outbox row exists for this turn**, recorded by the statement
that inserts it. Not `sent_at`, which is the drain's answer to a different question — an unsent row
still means the room is owed the text and must not be queued a second time — and not the "we
attempted" a delivery call used to report. `said_anything` is its own field rather than `spoke`
reused, because a session with no room queues nothing: conflating them was what let a resumed SPA
turn mint a second message for `result.result`.

What is left for stage 4: `adopt_open_turn` still asks the frames _which_ of its three cases a turn
is, and the loop still holds each value between two writes of it rather than folding a frame into
state. The row is now both halves of what the fold needs — `first_frame_seq` says where the turn's
frames begin, the three columns say what projecting them has produced — so what stage 4 adds is the
cursor between them.

### 4. The fold

`_run_turn`'s frame `match` becomes `project`, with the cursor advanced beside its effects. Mostly
moving code once stage 3 has made the state durable. The abort path collapses here too: an abort
becomes an intent the transport writes, and the CLI's answer comes back as frames — which is what
removes the "exactly one `anext` in flight" dance.

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
