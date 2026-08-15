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

### E3. Assistant messages arriving during the abort drain are discarded entirely

`claude_chat.py`, the abort drain in `_run_turn` (`if remaining.get("type") == RESULT_FRAME_KIND`). It looks only for `RESULT_FRAME_KIND`; an `assistant` frame
arriving between the interrupt and the result — the normal case, since the CLI finishes the message
it is mid-way through — is thrown away. No `update_assistant`, no row, no delivery. The text exists
only in the rollout (the recorder is at the transport layer, so the frame _is_ in
`session_frames`) and appears nowhere an operator looks.

Needs an abort, not a reconnection. Nothing logs it. Structurally untested: `_InterruptedCli.frames`
sets the abort only after its whole script has been yielded, so the drain never sees an `assistant`
frame. **An outbox does not close this** — the reply never reaches the delivery layer at all.

### E4. A turn that raises after streaming loses what it produced

`claude_chat.py`'s `if result.get("is_error") and not abort_event.is_set():` raises _before_ `final_text` is computed and before any
delivery; so do failures in the writes above it. The `except` closes the turn `FAILED` and re-raises,
`handle_runner` fails the session. Streamed text is in `session_messages` and the rollout, never
in the room. The supervisor announces the failure (`matrix_session.py:368-378`), so the operator sees
_a_ failure — not that an answer was produced and stranded.

Closed by an outbox **only if the row is written where the reply is produced** (in the same
transaction as `update_assistant`), not at delivery time. That is the design constraint stage 5
should adopt.

### E5. An empty reply is swallowed — R11.2 is not satisfied

`matrix_session.py:246-249` returns without sending when `text.strip()` is empty. A turn that ran
only tools produces no room event at all. R11.2 says every turn speaks and there is no silence
token; the code has one, and it is the empty string.

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

### I3. The batch is acknowledged at enqueue, not at turn completion — R2.5 is not satisfied

`save_batch` runs as soon as `offer` returns, which is as soon as `enqueue_prompt` commits. A
session dying between enqueue and the turn orphans the prompt: `expire_stale_leases` fails the
session, the supervisor creates a **new** `session_id`, and the old prompt row is keyed to the dead
one, so `next_prompt` never sees it. Acknowledged on the homeserver, queued in a dead session,
never answered. The room hears only "session ended … starting a new one".

Needs a session death, so a weaker candidate here — the plainest requirement violation on this side.

### I4. Backfill page cap

`matrix_client.py:447-455`: past `MAX_BACKFILL_PAGES` (20 × 100) the gap is abandoned with an error
log and the watermark still advances. R1.7's "say so loudly" is satisfied; the messages are gone.
Needs a ~2000-event gap.

### I5. `offer` only catches `RuntimeError`

`matrix_session.py:177-181`. `enqueue_prompt` raises `KeyError` when the session row is gone; that
propagates to `_run_as_leader`'s handler, which logs and sleeps `ERROR_BACKOFF`. No message is lost
— the watermark is untouched — but the batch stalls in a retry loop with a generic line and no
`holding` notice, which looks like a drop from the room.

## Requirement verdicts

| Req                                               | Status                                   | Evidence                                              |
| ------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------- |
| R1.6 no inbound message silently dropped          | Fixed (#4087)                            | was `matrix_client.py:422,440` + `matrix_sync.py:323` |
| R1.7 downtime recovery, never skip silently       | Satisfied in spirit                      | `matrix_client.py:447` logs loudly, still advances    |
| R2.5 batch acknowledged after its turn completes  | **Not satisfied**                        | `matrix_sync.py:318-323` acks on enqueue              |
| R11.2 every turn speaks                           | **Not satisfied**                        | `matrix_session.py:247-249`                           |
| R11.6 produced reply never lost silently, retried | **Not satisfied, nothing implements it** | `matrix_pacer.py:152-162`; no outbox table anywhere   |

## What the outbox closes, and what it does not

**Closes** E1, E2's remainder, E6, E7, E9 and lease-expiry stranding — every path where a reply
reached the delivery layer and the process lost it. `EventTag.transaction_id()`
(`matrix_client.py:126-138`) already makes redelivery idempotent server-side, so a redrive sweep is
safe today. One design constraint: **write the outbox row in the same transaction as
`update_assistant`**, not at `_deliver_reply` time, or E4 stays open.

**Does not close** E3 (the drain never produces the reply), E5 (an empty-turn policy question), and
I1–I4 (ingress acknowledgement semantics). Each needs its own fix: process `assistant` frames in the
abort drain; say something for an empty turn; enqueue-or-refuse unmappable event types; move
`save_batch` behind turn completion.
