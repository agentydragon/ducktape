# Where a Matrix message goes missing

Investigation of a long-standing report: replies that never appear in the room, in sessions where
**no runner reconnection and no console roll happened**. That rules out the two explanations the
plans already carried (the record-versus-deliver gap in `RolloutRecorder.received`, which needs a
replay; and a pacer queue lost with its process, which needs a death), so the cause is elsewhere.

Read of `haku/console/x/{claude_chat,matrix_sync,matrix_pacer,matrix_client,matrix_session}.py` at
`devel`, 2026-08-15. Line numbers are where each thing stood then; the symbol names are what to
grep for once they have drifted.

## The one structural fact everything below follows from

**Delivery is fire-and-forget, and the producer's `await` returns before any HTTP request exists.**
`MatrixSyncService.reply` (`matrix_sync.py:147-163`) builds a closure and calls `pacer.send(post)` —
a synchronous `deque.append` — then returns. `MatrixSurface.deliver` returns, `_deliver_reply`
returns, `_run_turn` marks the turn answered. The `room_send` has not been attempted yet, and
nothing that later happens to it can reach the turn loop.

So the turn loop's idea of "the room heard this" is, at best, "a closure was appended to a deque".

## Egress: a reply was produced and the room never got it

Ranked by how well each explains a drop with no reconnection and no roll.

### E1. A queued send that raises is logged and discarded — no retry, ever

`matrix_pacer.py:152-162` pops the slot at 147, and any exception from `slot.send()` destroys it.
Reachable without anything unusual happening:

- **429 past `MAX_RATE_LIMIT_RETRIES = 2`** (`matrix_client.py:83`). Deliberate — a 429 "has to
  reach this object to be learned from" (`matrix_pacer.py:24-26`) — but the pacer learns the rate
  and loses the message that taught it. The budget is a real 0.2/s that two replicas each believe
  they own in full, so sustained 429s are expected rather than exotic.
- **Any 5xx or transport error**: `_unwrap` (`matrix_client.py:220-226`) turns every `ErrorResponse`
  into `MatrixError`.
- **`await self._token()` raising inside the closure** (`matrix_sync.py:161`). Token resolution — a
  `whoami`, possibly a `login`, and Synapse rate-limits `/login` — happens on the pacer task. The
  sync loop's `except MatrixAuthError` re-login handler (`matrix_sync.py:367`) is a different task
  and never sees it.

Surfaced only as `logger.exception` in the console pod. The room is silent, the transcript row says
the message completed, the turn says `ANSWERED`.

`test_matrix_pacer.py:106-121` asserts this behaviour (`sent == ["before", "after"]`). Its framing
is right — one failure must not stop the queue — but it also blesses losing the payload.

**This is the leading explanation for the operator's reports.**

### E2. `spoke` meant _attempted_, not _delivered_ — FIXED

`_deliver_reply` swallowed every exception and returned nothing; `_run_turn` set `spoke = True`
regardless; `if not spoke:` then suppressed the `result` frame's copy of the same text — the room's
only remaining chance at it. One refused send silenced a whole turn.

Fixed by returning delivery success and assigning `spoke` from it, with
`test_a_send_that_failed_does_not_count_as_the_room_having_heard_it` as the regression. **Partial
by construction**: given the structural fact above, the Matrix surface reports success at enqueue,
so this only closes failures the surface can actually see. It removes one of the two layers of the
lie; E1 is the other.

### E3. Assistant messages arriving during the abort drain are discarded entirely — FIXED

`claude_chat.py`, the abort drain in `_run_turn` (`if remaining.get("type") == RESULT_FRAME_KIND`). It looked only for `RESULT_FRAME_KIND`; an `assistant` frame
arriving between the interrupt and the result — the normal case, since the CLI finishes the message
it is mid-way through — was thrown away. No `update_assistant`, no row, no delivery. The text existed
only in the rollout (the recorder is at the transport layer, so the frame _was_ in
`session_frames`) and appeared nowhere an operator looks.

Needed an abort, not a reconnection. Nothing logged it. **An outbox did not close this** — the reply
never reached the delivery layer at all.

Fixed by **deleting the second loop**: the interrupt now sets a flag that stops the one loop racing
the abort event, and every frame after it is folded in by the same `match` as every frame before it.
So there is no separate account of what an `assistant` frame means, which is the failure mode that
produced this one (<../../plans/chat_runtime_projection.md>'s two state machines). A drained message
counts towards `spoke` and `saw_assistant_message` exactly as a mid-loop one does, so the room is not
also owed `result.result` (which repeats it) and no second message row is minted for it — leaving
`ABORTED_NOTICE` to be said on its own, as the single `turn_id`-keyed outbox row the turn writes, so
`uq_session_outbox_turn` cannot be violated. Abort semantics are unchanged: the turn still ends, the
notice is still said, the session survives.

It was structurally untested — `_InterruptedCli.frames` sets the abort only after its whole script
has been yielded, so the drain never saw an `assistant` frame. `_CliFinishingItsMessage` queues one
ahead of the interrupt's `result`, and
`test_a_message_the_agent_finished_before_stopping_survives_the_drain` is the regression.

### E4. A turn that raises after streaming loses what it produced

`claude_chat.py`'s `if result.get("is_error") and not abort_event.is_set():` raises _before_ `final_text` is computed and before any
delivery; so do failures in the writes above it. The `except` closes the turn `FAILED` and re-raises,
`handle_runner` fails the session. Streamed text is in `session_messages` and the rollout, never
in the room. The supervisor announces the failure (`matrix_session.py:368-378`), so the operator sees
_a_ failure — not that an answer was produced and stranded.

Closed by an outbox **only if the row is written where the reply is produced** (in the same
transaction as `update_assistant`), not at delivery time. That is the design constraint stage 5
should adopt.

### E5. An empty reply is swallowed — FIXED (#4088)

`matrix_session.py:246-249` returned without sending when `text.strip()` was empty. A turn that ran
only tools produced no room event at all. R11.2 says every turn speaks and there is no silence
token; the code had one, and it was the empty string.

Fixed in #4088: an empty turn announces `NOTHING_SAID` as an `m.notice` under
`RoomEventKind.NARRATION` rather than returning. A notice and not a reply, because nothing was
said — it is the console reporting an outcome, not the agent talking.

### E6. Overflow drops the arriving send at `MAX_QUEUED_SENDS = 200`

`matrix_pacer.py:88-90`, logged at `error`. 200 is far above a turn's worth, so this means the room
has been unreachable a while — but it drops the _newest_ reply rather than the oldest stale
narration. Not a likely explanation for scattered drops.

### E7. Shutdown drops everything past the 5-second flush

`matrix_pacer.py:57,174-180`. At 0.2 sends/s, five seconds is **one send**. The ordering is sound
(uvicorn's 10s graceful shutdown → lifespan → 5s flush, inside `terminationGracePeriodSeconds: 25`,
with `session_service.aclose()` before the flush), so the budget is coherent — the rate is what
makes it thin. Requires a roll, so it does not explain these reports.

### E8. `drop_status` can declare idle while a send is in flight

`matrix_pacer.py:115-119` sets `_idle` when the queue empties, but `_drain` may be parked inside
`await slot.send()`. `flush()` then returns early and shutdown cancels an in-flight send. Narrow in
practice — `clear_status` immediately queues the retire, re-clearing `_idle`.

### E9. No room bound

`matrix_sync.py:154-157` (reply, `logger.error`) and `261-272` (announce). Only reachable with a
live session whose `matrix_conversation` row is missing. Loud.

## Ingress: an operator message that never became a prompt

### I1. Non-`m.text` events are dropped silently, and the watermark advances — FIXED (#4087)

`matrix_client.py:419-425` and the same filter in `_backfill` at 440 keep only `RoomMessageText`.
An `m.image`, `m.file`, `m.audio`, `m.video` or `m.emote` is discarded **with no log line**, and
`sync_once` then reaches `save_batch` (`matrix_sync.py:323`) and acknowledges it forever. The
operator sends a screenshot; nothing is written anywhere and it can never be recovered.

The unambiguous violation of R1.6 ("no inbound message is silently dropped"). No reconnection
needed. No test. An outbox does not help — the filter has to either enqueue an unmappable-event
notice or refuse the batch.

Fixed in #4087 by surfacing rather than refusing: an unreadable event is carried out of the sync
as an `UnmappableEvent`, said out loud in the room, and then acknowledged. Refusing would not
converge — nothing about an already-sent screenshot ever changes, so the batch would be re-offered
forever and one image would wedge ingress against every later message. That change also corrects
this section on one point: **`m.emote` is prose**, not unmappable — it is `m.text` in the third
person with the words in `body` — and it is now serviced rather than reported.

### I2. The accept-or-refuse path is sound

Checked because it was the obvious suspect, and it holds: `sync_once` (`matrix_sync.py:317-324`)
leaves the watermark untouched whenever `offer` refuses, and `MatrixTurns.offer`
(`matrix_session.py:159-182`) is genuinely all-or-nothing — one `enqueue_prompt` over the whole
batch, one transaction. No partial-acceptance case exists.

The loss that does exist is upstream: `_serviced` (`matrix_sync.py:282-300`) drops non-live-room
messages with a warning and the watermark advances regardless; when it returns `[]` the entire batch
is discarded and acknowledged. Warned, so not silent — still unrecoverable.

### I3. The batch is acknowledged at enqueue, not at turn completion — FIXED (#4117)

`save_batch` ran as soon as `offer` returned, which is as soon as `enqueue_prompt` commits. A
session dying between enqueue and the turn orphaned the prompt: `expire_stale_leases` fails the
session, the supervisor creates a **new** `session_id`, and the old prompt row is keyed to the dead
one, so `next_prompt` never sees it. Acknowledged on the homeserver, queued in a dead session,
never answered. The room hears only "session ended … starting a new one".

Needed a session death, so a weaker candidate here — the plainest requirement violation on this
side.

Fixed by **deferring the acknowledgement to the turn, without holding the batch here either**. A
batch that reaches a session leaves a `matrix_held_batch` row carrying the `/sync` token it ended
at and the transcript row `enqueue_prompt` minted; the watermark stays where it was until the
prompt's fate (`SessionStore.prompt_fate`) comes back. `COMPLETED` — the turn ended, whatever its
`TurnOutcome` — publishes the held token; `LOST` — the session ended without the prompt ever
reaching a turn that ran — drops the row and leaves the watermark, so the next pass offers the same
messages to the replacement session. Nothing is copied anywhere: the homeserver is still the queue,
exactly as it is for a refusal.

Two things fell out of it that are worth knowing before changing this code:

- **The loop reads ahead of what it promises.** Polling from the watermark while holding it would
  re-deliver the batch a session already has on every pass — and `/sync` long-polls only for data
  the caller has _not_ been sent, so it would return instantly for the whole length of a turn. The
  held token is the poll cursor; the watermark is the promise. That also means there is no
  message-level dedupe to get right: the delivered events are simply behind the cursor.
- **`FAILED` and `ABORTED` acknowledge.** Holding out for `ANSWERED` would wedge ingress behind the
  first turn that does not produce one — the same non-convergence that made an unreadable event
  something to announce rather than refuse (I1). The cost is explicit: a turn that ran and died
  takes its batch with it, which is exactly what R2.5 licenses ("losing an in-flight turn to a
  crash is acceptable") and what R8.4 tells the agent to expect.

### I4. Backfill page cap

`matrix_client.py:447-455`: past `MAX_BACKFILL_PAGES` (20 × 100) the gap is abandoned with an error
log and the watermark still advances. R1.7's "say so loudly" is satisfied; the messages are gone.
Needs a ~2000-event gap.

### I5. `offer` only catches `RuntimeError` — FIXED (#4092)

`matrix_session.py:177-181`. `enqueue_prompt` raises `KeyError` when the session row is gone; that
propagated to `_run_as_leader`'s handler, which logs and sleeps `ERROR_BACKOFF`. No message was lost
— the watermark is untouched — but the batch stalled in a retry loop with a generic line and no
`holding` notice, which looks like a drop from the room.

Fixed in #4092: `offer` catches `(RuntimeError, KeyError)` and refuses, so a session row that has
gone reads as "not now" like any other refusal and the room gets its `holding` notice. The same
change deleted `offer`'s status pre-check — admission is `enqueue_prompt`'s alone, decided under
`SELECT … FOR UPDATE`, so a status read outside it could only agree with a decision not yet made.

## Requirement verdicts

| Req                                               | Status                   | Evidence                                              |
| ------------------------------------------------- | ------------------------ | ----------------------------------------------------- |
| R1.6 no inbound message silently dropped          | Fixed (#4087)            | was `matrix_client.py:422,440` + `matrix_sync.py:323` |
| R1.7 downtime recovery, never skip silently       | Satisfied in spirit      | `matrix_client.py:447` logs loudly, still advances    |
| R2.5 batch acknowledged after its turn completes  | Fixed (#4117)            | was `matrix_sync.py:318-323`, acked on enqueue        |
| R11.2 every turn speaks                           | Fixed (#4088)            | was `matrix_session.py:247-249`                       |
| R11.6 produced reply never lost silently, retried | Fixed (`session_outbox`) | was `matrix_pacer.py:152-162`                         |

## What the outbox closes, and what it does not

**Closes** E1, E2's remainder, E4, E6, E7, E9 and lease-expiry stranding — every path where a
reply reached the delivery layer and the process lost it. The design constraint this section
called for held: the row is written **in the same transaction as `update_assistant`**, not at
`_deliver_reply` time, which is what takes E4 with it.

**One claim above was wrong, and it is worth reading before trusting the rest.**
`EventTag.transaction_id()` (`matrix_client.py:126-138`) does _not_ make every redelivery
idempotent: it derives from the transcript row where there is one and **mints a fresh `uuid4()`
otherwise**, so a redrive of a turn's abort notice, or of text that arrived only on a `result`
frame, would have posted a second message. Every outbox row is now sent under its own id, and two
partial unique indexes (`message_id`, `turn_id`) stop a second row existing for one logical
reply — the second of those because writing a turn's last word before closing the turn, which is
what keeps it from being stranded, is also what lets a replacement replica re-derive it.

**Does not close** E3 (the drain never produced the reply) or I2–I4 (ingress acknowledgement
semantics). E3, E5, I1 and I3 have each since been fixed in a change of their own, which is the
shape the rest should take. What is left on this list is I2's other half — a batch whose messages
all come from an unserviced room is still discarded and acknowledged, warned about but
unrecoverable — and I4's backfill page cap.
