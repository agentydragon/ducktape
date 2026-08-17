# Every outbound Matrix write, and which of them our database has never heard of

The invariant, stated by the operator on 2026-08-15 and not written down anywhere until now:

> No events should be written directly into Matrix without going through our database. Because
> Matrix is just one of pluggable backends. Channels.

Read as a rule about writes: **every outbound thing a channel shows must be recorded in our own
store first, and reach the channel from that record.** A write that goes straight to the
homeserver is invisible to every other channel, unrecoverable after a crash, and un-projectable —
the class of bug <message_drops.md> is about.

Read of `haku/console/x/{matrix_client,matrix_sync,matrix_session,matrix_pacer,room_status}.py`
and `haku/console/x/claude_chat.py` at `devel`, 2026-08-16. Line numbers are where each call
stood then; the symbol names are what to grep for once they have drifted.

**Paths below are pre-move.** The runtime is `x/session_runtime.py` and the Matrix modules are
`x/channels/matrix/{client,sync,session,pacer,outbox}.py` since the layout split; the symbol names
are unchanged and are what to grep for.

## The write surface is six calls

`MatrixClient` is the only holder of a Matrix credential, and it exposes exactly six calls that
change state on the homeserver: `join`, `send_text`, `send_notice`, `edit_notice`, `set_typing`,
`redact`. Everything else on it (`login`, `whoami`, `sync`, `recent_messages`) reads.

Worth recording as a negative result, because these are the writes that are easy to forget and
they are genuinely absent: **no read receipts** (`m.receipt` / `m.fully_read` are never sent), no
display-name or avatar write, no topic or any other `m.room.*` state event, no invite issued by
us, no `leave`. `redact` is used once and only against our own status line. So the forgettable
half of the surface is empty here, and the whole audit is the six calls above.

## The table

| #   | Write                          | Call site                                     | Tag kind     | Verdict               |
| --- | ------------------------------ | --------------------------------------------- | ------------ | --------------------- |
| 1   | `send_text` — a reply          | `matrix_sync.py:246` `post_reply`             | `reply`      | **recorded**          |
| 2   | `send_notice` — setup line     | `matrix_sync.py:368` ← `MatrixSurface.report` | `narration`  | **recorded** (see §2) |
| 3   | `set_typing`                   | `matrix_sync.py:307` `set_typing`             | —            | **ephemeral**         |
| 4   | `send_notice` — status create  | `matrix_sync.py:285` `show_status`            | `status`     | **ephemeral**         |
| 5   | `edit_notice` — status change  | `matrix_sync.py:289` `show_status`            | `status`     | **ephemeral**         |
| 6   | `redact` — status retirement   | `matrix_sync.py:333` `clear_status`           | —            | **ephemeral**         |
| 7   | `join` — `m.room.member`       | `matrix_sync.py:234` `_handle_invite`         | —            | **bypassing**         |
| 8   | `send_notice` — invite refused | `matrix_sync.py:231` `_handle_invite`         | `room`       | **bypassing**         |
| 9   | `send_notice` — joined a room  | `matrix_sync.py:236` `_handle_invite`         | `room`       | **bypassing**         |
| 10  | `send_notice` — adopted a room | `matrix_sync.py:480` `_live_room`             | `room`       | **bypassing**         |
| 11  | `send_notice` — rejected N     | `sync.py` `_report_rejected`                  | `rejected`   | **recorded**          |
| 12  | `send_notice` — unreadable     | `sync.py` `_report_unreadable`                | `unreadable` | **recorded**          |
| 13  | `send_notice` — lifecycle      | `matrix_session.py:362,403` → `announce`      | `lifecycle`  | **bypassing** (worst) |
| 14  | `send_notice` — silent turn    | `matrix_session.py:274` → `announce`          | `narration`  | **bypassing**         |
| 15  | `send_notice` — turn aborted   | `session.py` `MatrixSurface.report_abort`     | `narration`  | **recorded** (§ 1)    |

**The bypassing writes are rows 7–10, 13 and 14.** Rows 8–14 share one send path —
`MatrixSyncService._queue_notice`, which
builds an `EventTag`, closes over a `send_notice`, and hands the closure to the pacer. What
separates 11 and 12 from the rest is no longer the path but what stands behind it: each is now a
`session_events` row committed before the notice is queued, so the send is a rendering rather than
the fact. They are listed separately because they are seven different facts with seven different
homes, and lumping them would hide that some are session events and some are channel-binding
events.

## 1. The recorded writes

**Row 1, a reply, is the pattern.** `#4104`
put it there: `update_assistant` writes the transcript row and the `session_outbox` row in one
transaction (`claude_chat.py:906-917`), and `RoomOutboxDrain` claims the row and sends _from it_
(`matrix_outbox.py:227-250`). Record first, reach the channel from the record, mark `sent_at` only
once `room_send` has returned. A second channel could be handed these rows tomorrow.

**Row 2, bootstrap narration, is recorded but not driven from the record.** `_progress_reporter`
(`claude_chat.py:1304-1319`) writes a `setup_output` frame row and _then_ separately calls
`MatrixSurface.report`, which queues an unrelated in-process closure carrying the same text. So
the fact is durable and the delivery is not: a replica dying between the two loses the room's copy
with nothing to notice it is missing. It is the right classification — the fact is in our store —
but it is fan-out, not projection, and it is the shape §1 of
<../plans/session_channels.md> exists to replace.

**Row 15, the abort notice, is the only one that is a projection.** `end_turn` writes a
`turn_aborted` row into `session_events` under the lock that closes the turn, and the surface
announces afterwards — so the fact is in the record, the room's copy is a rendering of it, and a
reconciler re-derives that rendering from the row rather than from a queue. It used to be a
`turn_id`-keyed `session_outbox` row, which made it the one durable non-reply artifact the console
kept and kept it in the channel's table.

## 2. The three ephemeral writes, and why the claim is defensible here

The typing indicator (3) and the status line (4–6) are **renderings of live state**, and I claim
ephemerality for them on a stronger basis than "it is only a typing indicator":

- **They are already argued out in writing, twice.** `../plans/session_channels.md` §3 says
  recording the status text would be recording a rendering and calls it "the one mistake this
  section exists to prevent"; `haku/plans/matrix_chat_runtime.md:817-819` says the status line and
  typing indicator "stay Matrix-only renderings of live state, and stay unrecorded".
- **The state behind them is recorded.** `room_status.coarse_status` derives the status text from
  the CLI frames that `RolloutRecorder` is writing to `session_frames` anyway. So the invariant
  holds in the form that matters: nothing in the room is a fact the database does not have. What
  is not recorded is the _rendering_ of it, which is per-channel by construction.
- **They have no convergent form.** `session_channels.md` §1 makes this point about the
  reconciler: redacting a status message and clearing a typing indicator have no cheap "what does
  the room currently show" to compare against, so they are driven by the turn rather than by a
  cursor.

**The honest cost of the claim**, which is the thing the brief asks be spelled out: a second
channel cannot reuse any of this. It gets the recorded frames and must write its own
`coarse_status` equivalent — and `coarse_status` reads Claude's wire format (`assistant` frames,
`system`/`task_started` subtypes), so today the derivation is welded to one AI backend as well as
one channel. That is the coupling `session_channels.md` §1 names as the way to get the reconciler
wrong, and it already exists on the rendering side.

> **Half of that has since been fixed** (the neutral projection, `x/README.md` § The neutral
> projection): `coarse_status` takes a run of `ConversationEvent`s, so the derivation is no longer
> welded to one AI backend. It is still welded to one channel — the driver lives in `room_status.py`
> beside the Matrix surface — so the reuse cost above stands, one axis smaller.

## 3. The eight bypassing writes, and what recording each would take

### Row 13 — lifecycle notices (the worst one)

`MatrixSessionSupervisor` announces every session transition: provisioning, live status, ended
with its reason, and supervisor handover (`matrix_session.py:353-362, 400-404`). These reach the
room and **nothing else**. Three separate consequences, which is why this is the worst row:

- **There is no history to derive them from.** `sessions.status` holds the current value and
  `outcome.error` is overwritten by the next transition. So "when did this session become
  `failed`, and why" has no answer once it has been replaced — the room's scrollback is the only
  record, and it is federated to servers we do not own.
- **The dedup is a per-process local.** `_last_announced` (`matrix_session.py:342`) means a leader
  handover re-announces the current status, which reads in the room as the session having changed
  when only the supervisor did. `session_channels.md` §3 already names this: under a cursor, the
  cursor _is_ the record of having announced it.
- **Before a room is bound they are dropped entirely.** `announce` logs "no room bound yet,
  dropping notice" and returns (`matrix_sync.py:364-367`). A session that was provisioned, failed
  and replaced before the operator ever invited Haku leaves no account of itself anywhere — the
  case that argues a console-authored session event has to be recorded somewhere durable at all.

**Where it belongs: `session_events` on the `authored` arm, not the frame log and not the outbox.**
It is a thing that happened, not a thing to send. This note first said the frame log, following
`setup_output`'s precedent for a console-authored row that is not the CLI's `type`; the operator
ruled that out on 2026-08-16 — frames come only from actual runner↔console communication, and a
lifecycle transition is not that. `setup_output` is not the counter-example it looked like: a
`SetupOutput` envelope does cross the wire.

**Why it is not fixed here.** The row needs a `kind`, and choosing one is choosing what a lifecycle
event _means_ channel-neutrally — `session_channels.md` §3 is explicit that `lifecycle` and
`narration` become channel-neutral session events while `status`, `holding` and `room` stay
Matrix's own, and warns against promoting the whole `RoomEventKind` enum. `AuthoredEventKind` is
where that vocabulary now goes; it carries the two lease facts and not yet a status transition.

### Row 14 — the silent-turn notice

`_speak` (`claude_chat.py:1735-1751`) tries `enqueue_turn_reply`; when the turn produced no text
that returns False and `report_silent_turn` posts `NOTHING_SAID` as a `narration` notice.

**Derivable, unlike lifecycle**: the turn row already says it, as `said_anything=False` and
`queued_reply=False`. So a second channel can render "this turn finished without saying anything"
from the record without any new write. It is still bypassing — the room event itself is durable and
un-replayable — but it is the cheapest row in the table to close.

**Where it belongs: the outbox, mechanically — and that is exactly why it must not be done
casually.** `session_outbox` has a `turn_id` idempotence key that this turn is not using, so the
row would slot in with no schema change at all. But `SessionOutbox` has no `kind` column ("every
row here is a `REPLY`", per its docstring), so the notice would go out as `m.text` under
`RoomEventKind.REPLY` instead of `m.notice` under `narration` — a visible change of msgtype and
tag, against the deliberate argument in `report_silent_turn`'s own docstring ("a notice rather than
a reply, because nothing was said") and against R7.4. Out of scope: it needs a `kind` on the
outbox, which is a schema change.

### Rows 11 and 12 — the rejection notice, and unreadable — **fixed**

Both were the console reporting on ingress with the fact in no store at the moment it was
announced. Row 11 was `_report_holding`, whose count existed only in the stack frame that built
it, deduplicated by a per-process `self._holding` with the same handover artefact as
`_last_announced`; row 12 was `_report_unreadable`, whose `UnmappableEvent` was dropped after the
notice was queued — so R1.6 rested on a room event and a log line, and a replica dying took the
drop with it.

Both are now `session_events` rows on the `authored` arm, written in the transaction that advances
the sync watermark: `PROMPT_REJECTED` carrying the reason and the text, `UNREADABLE_INPUT` one per
event carrying its media type. Holding as a state is gone with them — a batch the session will not
take is rejected rather than held, so what row 11 announced is now one message being answered
rather than a count of messages waiting.

**One case still bypasses**, and it is not fixable here: a room whose session has not been
provisioned yet has no session for the row to name, so that rejection is announced and kept
nowhere. It wants the entity above the session that `session_channels.md` §1's attachment implies
and nothing has built.

### Rows 8, 9, 10 — the room-binding notices

"invited to another room; still serving this one", "joined — this is now Haku's room", "adopted
this room — Haku had no room bound". The binding _decision_ behind each of these is durable —
`MatrixConversationStore.claim_room` writes `matrix_conversations` and the atomic
`on_conflict_do_nothing` is what decides the refusal (`matrix_session.py:112-130`). The
announcement of it is not.

**Where they belong: nowhere neutral.** `session_channels.md` §3 puts `room` on the Matrix side of
the vocabulary split — a room binding is not a session event, it is this channel's attachment
changing. Under §1 that is the per-attachment cursor's business
(`chat_attachment(session_id, surface, address, attached_at, detached_at)`), which does not exist
yet. Recording them today means either a Matrix-shaped table or a Matrix-flavoured frame kind,
and both prejudge the attachment model.

### Row 7 — `join`

The one write here that is not a message, and it is bypassing in a different way from the rest.
The order is right: `claim_room` commits the binding (`matrix_sync.py:226`) and only then does
`join` run (`:234`). So the decision is recorded before the effect.

But the effect is not driven from the record and nothing repairs it. **If `join` raises, the row
says Haku is bound to a room it is not in**, and there is no retry: the exception propagates to
`_run_as_leader`, which logs and backs off, and the next `/sync` will not re-offer the invite
because `_handle_invite` only fires for invites still in `rooms.invite` — which they are, so it
does in fact recover here, but only by accident of Synapse keeping the invite pending. The
membership change is durable, operator-visible and federated, and our store has no column that
says whether we made it.

**Where it belongs: a column on `matrix_conversations`, not the outbox or the frame log** — this
is attachment state, the same object §1's cursor hangs off. Out of scope: schema change.

## Verdict: nothing is fixed in this PR, and that is the finding

Every bypassing row above is blocked on exactly one of the two things the brief declares out of
scope, and the blockage is structural rather than incidental:

- **The only durable outbound record in the schema is reply-shaped.** `session_outbox` has no
  `kind` column, deliberately and documented ("every row here is a `REPLY`"). So nothing that is
  not a reply can be recorded as a thing-to-send without a migration.
- **The only durable "this happened" record is `session_frames`, whose `kind` is free text — and
  choosing a value for it is choosing what a neutral session event means.** That is
  `claude/haku-neutral-events`'s decision, and pre-empting it with a Matrix-flavoured kind is the
  specific mistake `session_channels.md` §3 was written to prevent.

There is no bypassing write with an obvious home that recording would leave behaviourally
identical. The nearest miss is row 14, which fits `session_outbox.turn_id` mechanically and would
still change the msgtype the room sees. So this lands as a document, and the fixes land behind the
vocabulary.

## What a second channel would need that Matrix currently gets for free

The point of the invariant. Telegram must be able to show the same conversation, so anything only
Matrix can do is a gap even when it works.

**1. A reply feed exists; nothing else does.** `session_outbox` is portable — `room_id` is Text and
its docstring already anticipates a discriminator beside it — so a Telegram drain could be written
against it today and would deliver every answer. It would deliver nothing else: no lifecycle, no
holding, no unreadable, no room binding, no silent-turn line. Matrix shows all of those because the
process that decides them also happens to hold the credential.

**2. Re-awakening reads the conversation back out of the homeserver, not out of our transcript.**
`MatrixSurface._recent` → `RoomChannel.recent_history` → `MatrixClient.recent_messages`, which
paginates `/messages` (`matrix_sync.py:337-355`, `matrix_client.py:330-368`). A replacement
session's context is **Matrix's copy** of the conversation, filtered by `_is_conversational`. This
is the invariant inverted: the channel is the source of truth for what was said, and the console
reads from it. A second channel has no such store (Telegram's bot API cannot page a chat's
history), so it would have to read `session_messages` — at which point two channels re-awaken
sessions from two different records that can disagree.

**3. Idempotence is Synapse's, and only replies own a key we control.** A reply's transaction id is
its outbox row id, stable across processes and redrives (`matrix_outbox.py:109-119`). Every notice
mints a **fresh `uuid4` per attempt** (`EventTag.transaction_id`, `matrix_client.py:133-145`), so
notices are not idempotent at all — the reason nothing double-posts is that nothing retries them.
A channel with at-least-once delivery would duplicate every one.

**4. Ingress backpressure is the sync watermark, and it is not ours.** The console has no durable
inbound queue on purpose: a refused batch simply leaves the watermark where it is and the
homeserver re-delivers (`matrix_sync.py:6-10`). That is a resumable server-side cursor, which
webhook-delivered channels do not have. A second channel needs a real inbound queue, and building
one would make queue-until-turn-end (R2.2) and R1.6 machinery we own rather than machinery we
inherit.

**5. Self-exclusion is a sender field.** `MatrixClient._read` drops everything whose sender is us
(R1.5), which is the first of the two guards against a self-feeding loop; `m.notice` being ignored
is the second. Both are Matrix affordances. `session_channels.md` §5's relay message — the console
posting the operator's own text — works _only_ because it goes out under `@haku` and is therefore
excluded from ingress by construction. A channel where the console posts as the operator would
loop, and there is no second guard to catch it.

**6. Rate budget, queueing and ordering are one Matrix-shaped object.** `matrix_pacer` hardcodes
Synapse's unoverridden `rc_message` defaults (0.2/s, burst 10) and enforces them with a single
deque per replica, plus one collapsing slot for the status line. A second channel needs its own
budget, and today the budget, the queue and the ordering guarantee are inseparable — the outbox
drain deliberately delegates _when_ to the pacer (`matrix_outbox.py:15-19`), so per-channel pacing
means splitting `RoomPacer` before it means anything else.

**7. In-place edit and redaction.** One status line per turn is `m.replace` plus `m.room.redaction`.
A channel without either renders live state some other way, which is fine — but it means the
status derivation has to be shared and the rendering must not be, and today `coarse_status` lives
in `room_status.py` beside the Matrix driver.

**8. A stuck typing indicator retires itself.** `TYPING_TIMEOUT_MS` is the homeserver expiring our
notice for us, which is the whole reason a console dying mid-turn is safe here
(`matrix_client.py:418-427`). That safety property is Synapse's, not ours, and a channel without it
needs an owner for "stop showing activity for a turn nobody is running".

**9. "Seen" is not a thing the console can say on any channel.** It writes no read receipts, so it
never knows and never asserts whether the operator saw a message. Matrix hides this: Element shows
the operator's own read state, so the room _looks_ like it has the concept. It has it because the
operator's client does, not because we do.
