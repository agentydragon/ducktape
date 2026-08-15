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

**1. Transport ↔ log.** The socket's only job: CLI frames into `claude_chat_frames`, deduplicated
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

### 1. The log stops having a hole — **landed**

Deltas were the one frame class not recorded, because the console kept a single rewritten `partial`
row instead. A fold cannot run over a log with a hole in it, and that hole is precisely why
`streamed` had to be carried by hand.

Cost, estimated and not yet measured: a long turn streams a few hundred deltas, so a row each is
tens of kilobytes. Worth checking against a real session's `count(*) where kind = 'stream_event'`
before stage 2 commits to it. `read_frames` leaves them out of its default view, which is where
"would bury the log for a reader" is actually answered. One extra write per delta until stage 2
removes the `partial` row it replaces, at which point it is a wash.

### 2. Retire the partial row

Tombstoned on `update_partial_frame`. Stop writing it, drop `clear_partial_frame`, then drop the
`partial` column and its two indexes — a release apart, because an old replica writing a row a new
one never clears would leave a stray in the rollout for good.

### 3. Turn state onto the turn row

The cheapest step with most of the benefit, and the one to do next. One additive migration; the
loop keeps its current structure and reads and writes its state instead of holding it. This alone
kills `TurnResumed`, `_said_anything`, `_streaming_assistant` and the `spoke` guess.

### 4. The fold

`_run_turn`'s frame `match` becomes `project`, with the cursor advanced beside its effects. Mostly
moving code once stage 3 has made the state durable. The abort path collapses here too: an abort
becomes an intent the transport writes, and the CLI's answer comes back as frames — which is what
removes the "exactly one `anext` in flight" dance.

### 5. The room outbox

`matrix_pacer`'s deque becomes rows. Delivery gains a retry and loses its guesswork.

## The one thing to keep in view

**The projector must be single-writer per session.** The lease already gives that, and it is the
reason none of this needs the fold to be re-runnable. If the lease's meaning changes — stage 5 of
<chat_runtime_cleanup.md> proposes that an expired lease should mean unowned rather than dead —
check this assumption against it rather than around it.
