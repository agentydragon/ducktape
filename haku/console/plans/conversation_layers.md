# The chat runtime in three layers

**Status: proposal.** Framing set by the operator, 2026-08-17:

> LLM session ↔ abstract conversation ↔ messaging channel

with the cardinality settled in the same breath:

> - **session**: runs as long as one runner
> - **conversation**: can span over multiple sessions; only one session holds it at a time
> - **matrix room**: I think we can assume that 1 matrix room == 1 conversation

<session_channels.md> § 1 proposes reconciling a channel against the session's record. This is the
model that proposal sits inside: what the three layers own, what crosses each boundary, and how a
turn's tool calls and thinking reach a room that will not take a token at a time.

## 1. What each layer owns

| Layer            | Owns                                                                                                                       | Identity today                | Ends when                                                 |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------- | ----------------------------- | --------------------------------------------------------- |
| **LLM session**  | the runner wire (`session_frames`), the model's context and its compaction, the sandbox claim, the lease one replica holds | `sessions.session_id`         | the runner goes — lease lapse, disconnect, failure, close |
| **Conversation** | what was said and done, ordered and backend-neutral: `session_messages`, `session_events`, `session_turns`                 | none of its own (§ 6)         | nothing expresses this                                    |
| **Channel**      | its own copy and addressing, its credential and rate budget, its rendering vocabulary, its delivery state                  | `matrix_conversation.room_id` | the attachment is released (R3.6c, unbuilt)               |

What sits across a boundary today rather than inside a layer, each a place where the model has work
to do:

- **The conversation's writer names a channel's address.** `_enqueue_reply`
  (<../x/session_store.py>) returns early on `chat.room_id is None` and writes `session_outbox.room_id`,
  inside the turn loop's own transaction. § 5.
- **The conversation's own read is addressed by a channel key.** `RoomTranscript.recent`
  (<../x/channels/matrix/session.py>) joins `Session.room_id`, so re-awakening asks the channel
  (`RoomChannel.recent_history`) for the conversation's tail. The sync loop's docstring already
  notes that the credential has nothing to do with answering it.
- **Channel state that survives nothing.** `MatrixSyncService._status_event_id`/`_status_body`,
  `MatrixSessionSupervisor._last_announced`, `MatrixSyncService._holding` — all per-process, so a
  leader handover re-announces and a rolled replica forgets which room event it was editing. § 3.
- **The room holds facts the record does not.** The holding count and the msgtypes of an
  unreadable event exist only in the stack frame that announced them
  (<../debug/channel_write_audit.md> rows 11–12). § 4.

## 2. The contract

### Session ↔ conversation

**Session → conversation** is the fold: `apply_frame` writes the message row, its `session_events`
rows and `sessions.projected_frame_seq` in one transaction. One writer at a time, because the lease
is single — which is what makes `event_seq` monotone per session, the premise the increment design
depends on (§ 3 below, and <session_channels.md> § 4's re-check).

**Conversation → session** is admission and re-awakening. `enqueue_prompt` accepts a prompt on a
ready session with nothing queued and refuses otherwise; a replacement session is handed the
conversation's tail (`RE_AWAKENING_MESSAGES`) as prompt text.

**Handover** is the operator's "only one session holds it at a time". Today that is the supervisor
plus the `MXSE` advisory lock; as a row it is a partial unique index (§ 6).

### Conversation ↔ channel

**Outbound** is projection: the channel is brought into agreement with the record (§ 4).

**Inbound is an offer, not a mailbox.** The channel's transport already holds unacknowledged
input, so admission answers rather than accepts — and a refusal is handled by not advancing the
watermark, which is why there is no second durable queue. Anything that replaces this edge must
keep refusal expressible.

### Subscriptions, in both directions

**Conversation → subscriber.**

- **The address is `session_events.event_seq`.** It is a global `Identity` sequence, so one
  session's rows are not contiguous: every read is "everything after N", never "the next one after
  N", and a gap is undetectable by construction.
- **The wake carries no payload.** `session_changed` names a session and nothing else; the
  subscriber reads. Level-triggered, edge-scheduled — <../x/session_live_updates.py> already builds
  this half: `LISTEN`/`NOTIFY` is broadcast, each replica fans out to the sockets it holds, and
  changes coalesce to at most one per session per half-second.
- **The position belongs to whoever needs it, and its shape follows from what they hold.** A tab
  holds no copy that outlives it, so its position is a query parameter and the server keeps
  nothing. A room holds its own copy, so its position is a durable cursor: a position behind the
  record is work the console still owes. Same stream, two consumer kinds, and the test that
  separates them is <session_channels.md> § 1's — does this subscriber hold a copy?
- **The address does not cover an open message.** A `TextDelta` is deliberately not a row, so a
  message being written is invisible in the log until `message_completed` while
  `session_messages.content` is mutated in place. A tab is sent that row whole; a room cannot be
  (§ 4).

**Channel → conversation.**

- **Two positions, not one.** `MatrixHeldBatch` exists because acceptance is not answering (R2.5):
  the loop polls from the batch it holds and acknowledges only up to the watermark. Generalized:
  any channel whose transport retains unacknowledged input needs a read position and an
  acknowledgement position, and the second is a promise rather than a cursor.
- **Today the retry is a poll.** A refused batch is re-offered after `UNADVANCED_BATCH_BACKOFF`
  (one second) because nothing tells ingress that the turn ended. Under reconciliation that is the
  same `session_changed` wake the outbound half uses, and the backoff becomes a fallback rather
  than the mechanism.

**The loop**, in the operator's terms — how far is the room behind the conversation, how far is the
conversation behind the room, send what can be sent both ways, otherwise wait:

```text
wake on: an inbound event, a session_changed, a freed send slot, or the fallback timer
  1. read the conversation from the cursor: events, the live turn, the open message
  2. read the room's own state: our tagged events (§ 3)
  3. inbound  — offer what the transport holds, if the conversation will admit it;
                on refusal leave the watermark where it is
  4. outbound — spend the sends the budget allows on the current difference
  5. wait
```

**Squashing is the semantics, not an optimization.** A queue under rate limiting delivers stale
intermediate states late; a reconciler recomputes the difference when it gets a slot and sends the
current truth, so ten notice updates collapse into one edit and the room never shows a state the
conversation has moved past. `RoomPacer.set_status` already replaces a status change that has not
gone out, and `drop_status` withdraws one — reconciliation for a single field, done imperatively
inside a queue whose vocabulary (`Send = Callable[[], Awaitable[None]]`) cannot express it for any
other.

## 3. The room as the channel's own store

`EventTag` (<../x/channels/matrix/client.py>) rides on every event the console sends, carrying
`kind`, `session_id`, `message_id` and `agent_message_id`. Its docstring calls itself "write-only,
for now" and names R11.3's room-read tools as the reader that would bring a parser back. **The
reconciler is that reader**: correspondence between the record and the room is exactly what step 2
of the loop needs, and the tag is already the correspondence.

- **Two readers of one `/sync`, with opposite filters.** R1.5 excludes Haku's own sender from
  input; today `MatrixClient._read` drops those events before anything else sees them, and
  `InboundMessage` deliberately carries no parsed tag. The correspondence reader is the mirror —
  only our sender, parse the tag, never a prompt. R1.5 constrains what may become input, not what
  may be read, so this is a new path at the client rather than a change of policy.
- **Redaction strips the tag**, since it lives in `content`. For a level-triggered reader that is
  the right behaviour — a retired status line's desired state is "none", and a redacted event reads
  as absence. The cost is that **the room is not an audit log**: nothing can ask it what it used to
  show, so any fact that must survive its own retirement is recorded conversation-side or not at
  all.
- **Idempotence by correspondence, with one window.** For anything the reader can see, the tag is
  the identity and a duplicate send is prevented by finding the event rather than by a transaction
  id. Between a send returning and its echo arriving in our own `/sync`, correspondence does not
  yet know the event exists — so the transaction id still guards inside Synapse's 30-to-60 minute
  dedup window (<../docs/chat_runtime_facts.md>), and a notice whose desired state is "exactly one
  live line" is self-correcting on the next pass only if the reconciler is allowed to redact its
  own duplicate.
- **The token cannot live in the room**, because it is what reads the room. So the channel keeps a
  private store whatever else moves; the only question is what else is in it. `matrix_sync_state`
  holds the token and the watermark in one row today — #4249 made both writers upsert after they
  collided, and #4252 splits the row into a credential cache and a watermark with one writer each.

**A prerequisite, not a follow-up: two outbound artifacts have no conversation-side identity.**
`EventTag.transaction_id()` derives from `message_id` where there is one and mints a fresh
`uuid4()` otherwise, so a resend of anything without a transcript row posts a second event. Two
such artifacts exist — a turn's abort notice, and text that arrived only on a `result` frame —
which is why `PendingReply.transaction_id` uses the outbox row's own id instead. A reconciler is
at-least-once by nature, so those identities have to exist before it does.
`SessionOutbox.turn_id`, with `uq_session_outbox_turn`, already keys one such artifact per turn:
the identity exists and is recorded in the channel's table rather than in the record. Moving it is
the work.

## 4. Projecting tool calls, thinking and session events into a room

A room takes prose. Everything else a turn does — a run of tool calls, a stretch of thinking, a
session being provisioned and replaced — reaches it as a **notice that updates as things happen**.
Naming its parts separately is what makes the projection reconcilable:

- **Its subject** — the span of the conversation it summarises: a turn, a tool-call run, a
  session's life.
- **Its body** — a pure function of that span's current state. Purity is what makes squashing
  correct: whatever the room last received, the next send is recomputed from the record.
- **Its lifecycle** — created when the span first has something worth saying, edited while the span
  is open, and closed when the span is.

### What streaming projects into

The conversation moves per delta; the room moves per message. Between two messages, what the room
can be told is that a turn is live (the typing notice, sent directly rather than through the pacer,
so it spends no queued slot) and how much has happened (the notice). When
`MessageCompleted` lands, the finished message is forwarded whole, as R11.1 already requires of
every assistant message rather than only the last.

**A message being written cannot be edited into the room**, and the layer argument is the one that
settles it rather than the rate budget: the conversation exposes no address for an open message.
`session_messages.content` is mutated in place and no event names its current state, so an
at-least-once reconciler has nothing to compare the room's copy against — it could only re-send the
whole prose and hope. That is the same fact that lets a tab be sent the open row whole (cheap,
idempotent, discarded when the tab closes) and stops a room being sent it (permanent, federated,
and re-published in full to every client that does not render edits, since an edit's fallback body
is the new text).

### What a notice body may do

- **Read the neutral vocabulary, never a backend's frames.** `coarse_status`
  (<../x/room_status.py>) already reads `ConversationEvent`; a notice that reached into Claude
  frames would weld the channel layer to one backend, which is the coupling
  <session_channels.md> § 1 exists to prevent.
- **Stay bounded independent of the span's length.** Forty tool calls summarise to a tally and the
  one in flight, not to forty lines: a room event is permanent and federated, and an edit
  re-publishes its whole body.
- **Stay coarse by rule (R6.3).** Where a tool is named, its identifier passes through verbatim;
  no per-tool copy, no mapping table. `ActivityStarted.description` is the harness's own prose and
  runs past 500 characters, so the notice truncates it.
- **Say that thinking happened, at most with its summary.** `Reasoning.summary` is the only
  renderable part, and a notice per `Reasoning` event is what the single line exists to avoid.

### A candidate shape, with today's status line as its degenerate case

**One work notice per turn.** While a turn is open the room shows at most one notice: what is
happening now, plus a bounded tally of what has happened already. Today's status line is this with
an empty tally, which makes the change additive rather than a replacement of R6's built behaviour.

**One notice per session for lifecycle.** Today each transition is its own `m.notice`, deduplicated
by a per-process `_last_announced` and dropped entirely before a room is bound. The facts belong to
the conversation as `AuthoredEventKind` rows (<session_channels.md> § 3); the room's rendering of
them is the channel's own decision, and once the timeline is in the record, collapsing the room's
copy into one edited line costs nothing that is not recoverable elsewhere. Under correspondence the
live notice's tag is the dedup state, so a leader handover stops re-announcing.

**Retire or seal is the open half of this.** A notice whose subject is live state should be
redacted when the state is gone (R6.5's argument for the status line: a spent line is clutter). A
notice whose subject is a fact that happened should be sealed with a final edit instead — retiring
it loses the account of what the agent did while working, which is what R11.1 was changed to stop
the room losing. Which notices exist, and which of the two each is, is left open (§ 7).

### What a notice does not need

**No outbox row.** Its durable form is the span it summarises plus the room's own tagged copy, so
nothing about it needs a second write on the send path. That is what keeps <session_channels.md>
§ 3's rule intact — record what happened, derive what is shown — and it is why the delivery queue
can be channel-private (§ 5): under reconciliation the queue holds no facts, only work derivable
from the record.

The exception is a notice whose fact is not in the record at all. The holding count and an
unreadable event's msgtypes are announced from a stack frame and kept nowhere
(<../debug/channel_write_audit.md> rows 11–12), so those two need a conversation-side writer before
they can be projected rather than sent.

## 5. Where the delivery queue belongs

**In the Matrix channel, as its private implementation detail.** `SessionOutbox` sits in the shared
schema (<../database_schema.py>) with a `room_id` column and a docstring anticipating "a
discriminator beside it" for a second channel. Under the layer model there is no second channel's
rows to hold: a channel that keeps no copy (the SPA) needs no queue at all, and a channel whose
transport has no idempotency key needs a different one — which is the case R11.6's "possibly
duplicated" marking comes back for (<../../plans/chat_runtime_projection.md> § 5).

**Moving it is the reconciler, not a rename.** The row is written today inside the turn loop's
transaction, so while it exists the conversation's writer branches on `chat.room_id`. Under
reconciliation the turn writes only the record and the channel derives what it owes, which is what
removes that branch. What stays shared is the record and the per-attachment cursor.

<../x/README.md> § Matrix chat surface currently calls `session_outbox` neutral and calls the
record-then-drain split "the shape every outbound channel write has to take". The split is right;
the neutral half is the record, and this is the sentence to correct when the move lands.

## 6. What a conversation would cost as a table

**It is not one today.** `matrix_conversation.session_id` is the pointer to the session serving the
room now; `sessions.room_id` is the history, written once so a past Matrix conversation stays
findable (R11.3a); and the conversation's own read — the tail handed to a replacement session — is
a join on `Session.room_id`. So the conversation exists, addressed by a channel's key.

What a real table would add: an identity independent of both neighbours, the ordered list of
sessions that have held it, and something for the per-attachment cursor to hang off.

**What it costs.** A `conversation` row plus a foreign key from `sessions`, backfilled from
`sessions.room_id` for Matrix sessions; every SPA session becomes its own one-session conversation,
which is what it already is. The migration is expand/contract — a mapped column takes three
releases to remove (README § Perimeter / deploy) — and it overlaps
<../../plans/chat_runtime_cleanup.md> § stage 7's `chat_attachment`, which already subsumes
`sessions.room_id` and `matrix_conversation.session_id`. So the real choice is whether attachments
are re-keyed to a conversation or a conversation table is added beside them.

**What not doing it costs.** Every cross-session read stays addressed by a channel key, so a second
channel onto one conversation cannot be expressed; and "only one session holds it at a time" stays
a property of the supervisor and its advisory lock rather than a partial unique index over open
sessions.

## 7. Open questions

- **Does the conversation become a real table, or does `chat_attachment` re-key onto one?** § 6
  prices both. Deciding it also decides where the reconciler's cursor lives.
- **Does the inbound watermark move into Matrix?** `m.fully_read` or per-room `account_data` would
  put it where recovery needs no console database, and the console sends no receipts today at all
  (<../debug/channel_write_audit.md>). Against: account data is per-user with no compare-and-set
  while the console runs several replicas as one Matrix user — a Postgres advisory lock serialises
  the writer today — and it is one position where R2.5 needs two. The credential stays in Postgres
  either way (§ 3).
- **Which notices exist, and what does each summarise?** § 4 proposes one per turn and one per
  session and leaves the set open, along with retire-or-seal for each.
