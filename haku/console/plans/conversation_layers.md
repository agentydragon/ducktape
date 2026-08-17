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

| Layer            | Owns                                                                                                                                                                                                                                                           | Identity today                | Ends when                                                 |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- | --------------------------------------------------------- |
| **LLM session**  | the runner wire (`session_frames`), the model's context and its compaction, the sandbox claim, the lease one replica holds, **and everything said or done while it ran** — `session_messages`, `session_events`, `session_turns` are all keyed by `session_id` | `sessions.session_id`         | the runner goes — lease lapse, disconnect, failure, close |
| **Conversation** | which sessions are the same thread, and which channels are attached to it — **identity, and nothing else** (§ 6)                                                                                                                                               | none today                    | never — a conversation has no end (§ 7)                   |
| **Channel**      | its own copy and addressing, its credential and rate budget, its rendering vocabulary, its delivery state                                                                                                                                                      | `matrix_conversation.room_id` | the attachment is released (R3.6c, unbuilt)               |

**An earlier draft of this table put the transcript under the conversation.** That was wrong and it
hid the shape of the problem: no row in the schema belongs to a conversation rather than a session,
so the conversation was being described as an under-built entity when it is a _view_. Correcting it
is what makes § 6's argument decidable — the question is not "where do the rows go" (they are
already where they belong) but "what names the set of sessions that are one thread".

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

**One primitive, and every channel is a consumer of it** (operator, 2026-08-17):

```text
subscribe(conversation, from: position | nothing)
  -> a snapshot, if no position was given
  -> then the changes, as they happen
```

That is the whole contract, and the point of naming it is that **the SPA and Matrix stop being two
mechanisms**. Today the browser reads the record and Matrix is pushed by the turn loop from inside
the process that holds the runner (§ 8) — one job done twice, which is why a fact can reach a room
and never reach a tab. Under one subscription there is a single path from the record outward, and a
channel differs only in two ways:

- **How it renders what arrives.** A run of tool calls is a list in a tab and one folded notice in a
  room (§ 4). That is the channel's own decision and it belongs there, not in the stream.
- **Whether its position is durable.** A room holds its own copy, so its position is a cursor the
  console owes work against; a tab holds none, so its position is an argument to the next read.
  Same subscription, different cursor lifetime.

The read half already exists in the shape the primitive needs: `GET /api/conversations/{id}` is the
snapshot and carries its `watermark`, `…/changes?after=N` is the changes, and `session_changed` is
the wake that says to ask again. What is missing is not a new protocol but a second consumer —
Matrix reading the record rather than being handed events by the turn loop.

**The stream carries two kinds of message, and a delta is one of them** (operator, 2026-08-17).
State changes are whole rows, merged by id. A **delta** is `{message_id, append}` — the neutral
`TextDelta` the fold already produces and never stores — and every consumer receives it: a room
ignores it because it cannot edit per token, a tab applies it and shows prose as the model writes
it. That is one mechanism with two message kinds, not a side channel; an earlier draft proposed
pushing deltas as a payload on the wake, and _that_ would have been a second mechanism.

**The row is the resync, which is what makes it safe.** `session_messages.content` is mutated in
place per delta, so a subscriber that misses deltas, joins mid-message or reconnects reads the
message row and is correct. No exactly-once, no ordering guarantee, no gap detection — the same
property that made whole-row replacement the right payload for state changes.

**Deltas are never stored.** The completed message carries the prose whole; a stored delta would
double the transcript for no reader. And note what this changes about pacing: `COALESCE_WINDOW`
stops being the rate prose arrives at and bounds only the state messages.

**v0 may ship without the delta kind** (operator, 2026-08-17). The increment already carries the
open message row whose `content` is the prose so far, so a subscriber that ignores deltas still
watches a message grow — at `COALESCE_WINDOW` granularity rather than per token. That is the
fallback if the delta kind costs more than it looks like it will, and it is a fallback rather than
the design because the design above is what the layering owes: the fold produces `TextDelta`
already, nothing about it is provider-shaped, and the row-is-the-resync argument is what makes it
cheap. Adding the kind later changes no schema and no stored row — only what the socket emits
between coalesced increments — so v0 shipping without it costs a follow-up, not a redesign.

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
- **The address does not cover an open message, and deltas are why that is survivable.** A
  `TextDelta` is deliberately not a row, so a message being written is invisible in the log until
  `message_completed` while `session_messages.content` is mutated in place. The delta kind rides
  the socket outside the address, which is exactly why it needs no gap detection: whoever misses
  one reads the row.

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
- **Its body** — a fold over the subscription stream (see below). Purity is what makes squashing
  correct: whatever the room last received, the next send is recomputed from what has arrived.
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

### A notice body is a fold over the subscription stream

**Every notice body is a reduction of the provider-neutral messages § 2's subscription delivers**
(operator, 2026-08-17). Not a query the channel issues against the store, and not a value the turn
loop hands it: the channel consumes one ordered stream and folds it into "what the room should
currently show". The channel's own state is the accumulator — the fold's carry plus what it last
sent — which is the same `(subject, body, tag)` triple the lifecycle above already names.

Three things follow, and they are why this is worth requiring rather than merely allowing:

- **It is testable without a room, a homeserver or a database.** A list of `ConversationEvent`s in,
  a notice body out; the edge cases that are hard to provoke end to end (a run of forty tool calls,
  a session replaced mid-turn, a turn that aborts between two tool results) become table rows. This
  is the one part of the channel where correctness is about accumulation rather than delivery, so
  it is the part worth isolating from the parts that need Synapse.
- **It is what makes reconciliation cheap.** An at-least-once reconciler needs to answer "does the
  room's copy match what it should show", and a fold gives that answer by construction: re-fold
  from the cursor, compare with the tag's last body, send only if they differ. Without a fold the
  comparison needs a second derivation of the same fact, which is the duplication § 2 exists to
  remove.
- **It forbids the shortcut that would rot.** A body computed from whatever the turn loop happened
  to be holding is a body no other consumer can reproduce — the exact coupling that lets a fact
  reach a room and never reach a tab (§ 8).

**v0 may emit one notice per noticeable event and never edit** (operator, 2026-08-17). That is a
lesser feature, not a different architecture: the fold still runs, the accumulator is still the
carry, and what changes is only that the channel appends its output instead of editing the open
notice's tag. Editing is added later by giving the fold's output a tag to reconcile against, which
touches the send path and nothing about the reduction. Take this if editing turns out to cost more
than it looks like it will — the tally-in-one-line shape below is the goal either way.

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

### The operator's own prompts are part of what a surface shows

**A prompt is a conversation fact, so every attached surface shows it** (operator, 2026-08-17) —
"send a prompt from the SPA and I'd expect the bot to deliver it to the Matrix conversation too",
and it is displayed in the SPA as a matter of course. § 11's acceptance behaviour 4 already
demanded this ("either can prompt, both show the same account"); this is the mechanism.

It follows from the subscription rather than being a feature bolted onto it. `PROMPT_ENQUEUED` is
already an `AUTHORED` event on the conversation, so a channel reading the record sees the operator's
half as well as the agent's. What is new is only that a channel must **project a prompt it did not
receive** — today the room shows the operator's messages because they were typed there, not because
anything projected them.

**The provenance pointer is what stops the echo, and that is its reader.** A prompt that arrived
through this room is already in this room; re-posting it would duplicate it. A prompt from the SPA,
or from another room, is not — so it must be posted. The test is "did this prompt originate on my
own attachment", which is exactly one comparison against the pointer § 9 step 5 adds:

- **Compare, never interpret.** The channel that minted the pointer is the only thing that reads
  inside it; every other consumer compares it for equality. That keeps § 11's invariant — no code
  outside a channel names a channel's address — while giving the field a real reader, which
  otherwise it would not have.
- **It must identify the attachment, not just the event.** Under § 7's ruling one bot holds several
  rooms, so "an event id I did not mint" is not enough to tell a sibling room's prompt from this
  room's. Once `chat_attachment` exists the honest shape is the attachment's own id plus the
  channel's opaque ref; until then it carries the address alongside the ref and the conversation
  treats both as opaque.
- **The SPA is named, not implied by absence** (operator, 2026-08-17). A prompt typed into a tab
  gets an origin that says so, rather than being the null case — so the pointer is a closed union
  over the surfaces a prompt can come from, and `enqueue_prompt`'s two callers are exactly its two
  live variants. The reason to prefer this over `None` is that absence has a second meaning it
  cannot shed: a row written before the field existed. Overloading one value with "typed in a
  browser" and "we do not know" makes the echo test silently wrong on old rows — it would post
  them to every room. Give the legacy state its own variant and tombstone it, gated on no row
  predating the field.

  The SPA's variant needs no address, and that is not an inconsistency: an address exists so a
  channel can tell its own copy from a sibling's, and a tab holds no copy to confuse. The Matrix
  variant carries address and ref; the SPA variant carries the surface alone.

**Rendering it is the channel's own decision**, like everything else in § 4: a room may want a
prompt from elsewhere marked as such, since an operator reading the room sees a message they did
not send there.

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

### `chat_delivery` is a revision index, and only some channels can keep one

Step 8's table (`0067`) is shared by every channel that attaches, and its shape assumes more about a
channel than the layer model lets us assume (operator, 2026-08-17, on a channel that is _"a teletype
printing everything as it happens"_, _"an old timey pager"_, or _"a telegraph key that sends a
constant message when pressed and doesn't display anything"_).

Three of its properties are Matrix's model, not a channel's:

- **`sent_ref` is `NOT NULL`** with a non-empty CHECK, so a channel with nothing to point at cannot
  record that it delivered anything at all.
- **`uq_chat_delivery_live_subject`** — one live row per `(attachment, subject)` — encodes _at most
  one artifact per subject, revised in place_. An append-only channel's correct behaviour when a
  subject changes is to emit again; the index either forbids that or forces a retirement that
  describes nothing.
- **`retire` means "the channel has taken this one back"**, which a channel with no display and no
  edit cannot do.

Two facts are fused here, and only one of them is neutral:

1. **"we already delivered subject S"** — every channel needs this, or a restart reprints the
   conversation and re-pages the operator at 3am.
2. **"…and it is still visible at ref R, so revise it there"** — a property of channels that hold an
   addressable, editable copy.

`chat_delivery` stores (2), and stores (1) only incidentally — inaccessibly to any channel that
cannot satisfy (2).

**For an append-only channel the table is not wrong-shaped so much as unnecessary.** What a teletype
needs is a position past which everything has been emitted, which is § 2's per-`(channel,
conversation)` subscription cursor — step 9's, and all such a channel needs. The correspondence
exists _because_ Matrix can revise, and revising requires addressing.

So the table stays and its name is the thing to fix: it is the revision index for copy-holding
channels, not a delivery log, and `x/delivery_log.py`'s own docstring already says "copy-holding"
while nothing in the schema or the module name carries that scope. This is § 11's rule one layer
down — the provider shape is living in a `NOT NULL` and a unique index, which is exactly the
load-bearing position that rule forbids. **Do not build the append-only path ahead of step 9**;
nothing today is a teletype, and a second mechanism invented now is one to delete later.

**Most of what it stores is write-only, which is the sharper form of the same finding** (operator,
2026-08-17, after #4307 merged: _"it felt to me like a strange thing that's trying to implement a
conversation updates cursor before we actually have it implemented"_). `PendingReply.subject()`
mints three kinds — `message:{message_id}`, `turn:{turn_id}` and the single `status` — so the table
takes one permanent row per assistant message per attachment. But the only reads anywhere are
`sync.py`'s two `live(attachment_id, STATUS_SUBJECT)` calls: **every `message:` and `turn:` row is
written and never read**, against <../../../STYLE.md> § Every field needs a reader.

For those rows the table adds exactly one fact over `session_outbox.sent_at`, which the drain
already writes in the same transaction: the room's `event_id`. Nothing edits a message, so nothing
wants that id — it is there for the reconciler that replaces the outbox, which does not exist. That
is what makes the objection right: the growing set of `(attachment, subject)` rows is a flushed-up-to
position materialised as a map, one row at a time, ahead of the cursor that will hold it properly.

**What genuinely earns a table is the one revisable subject.** `status` is edited in place and
retired; it is the only row whose `sent_ref` is read, and one live row per attachment is bounded.
So the narrowing to consider is not a rename alone: restrict what may be written to subjects the
channel can actually revise, and let the outbox stay the record of what has been sent until step 9's
cursor takes it. Doing that before more callers arrive is cheaper than after — today there are two
readers and one writer.

### A session has no frontend, and the port is per attachment

**"The frontend this session is attached to" is not a thing** (operator, 2026-08-17: _"what is a
session's frontend? why would a session care?"_). It is worth stating flatly because the code
already reads as though it were, and #4290 was closed for making it more explicit rather than less.

What is actually there: `SessionService` holds **one** `ChatFrontend`, the Matrix one, and
`_frontend_for` is not a lookup but a filter — is this session's `surface` the one my single
frontend serves? The name promises a mapping where there is a global and a guard.

**A session does not care, and nothing about a session should.** What cares is the turn loop, and
only because the turn loop is doing the channel's job: `report`, `report_silent_turn`, `_speak`
and `TurnStatus` are § 8's "pushed by the turn loop from inside the process that holds the runner",
which is why a fact can reach a room and never a tab. Step 9 deletes all of it — a subscriber reads
the record and nothing hands a frontend to a turn. The one use that is honestly surface-dependent
is `system_prompt`, telling the model it is speaking in a room, and that is a question asked once
when a session starts rather than a channel the turn holds.

**So the shape is a frontend per attachment, never per surface.** Selecting by surface is the
inversion made concrete — it says a channel owns a set of sessions, when a channel is attached to a
_conversation_ and a session merely happens to be the one running under it. It also does not
survive § 7's ruling: with one bot in several rooms every Matrix session matches the surface, so a
surface-keyed singleton cannot address any of them correctly. That is the checkable form of the
objection, and it is why the answer is `chat_attachment` (§ 6) plus step 9, not a discriminator on
the port.

**Until then, leave it alone.** The right amount of work on `_frontend_for` before step 9 is none:
it is deleted, not improved, and polishing it buys a rename in exchange for entrenching the concept.

## 6. The conversation is a real table, and it is identity only

**Decided 2026-08-17**, after the operator named the case that forces it.

### What forces it is a combination, not a cardinality

Taken one at a time, neither multiplicity needs an entity:

- **One session, many channels.** The session is already the object both attachments point at.
  Nothing to name.
- **Many sessions, one channel.** The address is the thread key: `(surface, address)` is stable
  across the sessions that served it, which is what `sessions.room_id` is doing today.
- **Many sessions × many channels.** Forced. When the sandbox dies and session A is replaced by B,
  what has to move is _the set of attachments_, and a set has no name. Re-pointing every one of A's
  live attachments at B works mechanically, but afterwards "this thread" is recoverable only as a
  transitive closure over "sessions that ever shared an attachment with…" — not a join, and no
  handle to link to.

The operator's case is the third: an SPA tab and a Matrix room both open on one session, both able
to send, and **"the sandbox went away, I had to restart it" preserved in both**. Session replacement
is not an edge case there — it is the supervisor's normal job.

### What it is

`conversation(conversation_id, operator_id, created_at)`, a foreign key from `sessions`, and
<../../plans/chat_runtime_cleanup.md> § stage 7's `chat_attachment` keyed on `conversation_id`
rather than `session_id`, keeping its partial unique index on `(surface, address) where detached_at
is null`.

**Identity and nothing else.** Every fact stays where it already is: what was said on the session,
delivery state on the attachment, rendering on the channel. That is what makes it feel like
ceremony, and it is the correct shape for naming a set whose membership changes over time — the
same call this codebase already made for `agents` against `credential_bindings`, where the Agent is
identity, the binding holds the state, and rotation creates a successor binding rather than
mutating the Agent (<../README.md> § Canonical Agent authority).

**What it buys is that the attachment stops moving.** Replacement becomes "a new session with the
same `conversation_id`"; the attachments are not touched, because they were never the session's.
Compare re-pointing them, which is the same write done N times and leaves nothing named afterwards.

**A session attached to nothing stays expressible** — a conversation with one session and no
attachment rows. That is what an SPA session is today, and it costs one row and no decisions.

### Where events live, which this does not change

On the session. "This session's sandbox died", "this replica adopted it", "the lease lapsed" are
facts about a session and stay `session_events` rows keyed by `session_id`. The conversation answers
only _which channels are told_. Events on sessions, fan-out by conversation.

### The cost

`sessions.conversation_id` is a mapped column, so removing it later would take three releases
(<../README.md> § Perimeter / deploy). The backfill is one conversation per session, except Matrix
sessions grouped by `room_id` and ordered by `created_at`, which the purge left small. It subsumes
`matrix_conversation` entirely — including its `user_id` primary key, which is what makes one bot
user serve exactly one room ever. Losing that rule is **the point rather than the cost** (§ 7): the
bot is meant to hold several rooms, and rooms held at once are sessions run in parallel.

### What does not wait for it

**Most of "both surfaces open on one session" needs no schema.** `enqueue_prompt` has no surface
check, so the browser can already prompt a Matrix-surfaced session given its id, and § 8's
increment route already serves it updates. What is missing is that `frontend/x/claude_chat_page.tsx`
_creates_ a session rather than joining one. The durable machinery here is for the channel that
holds its own copy surviving replacement — not for the browser, which holds none and converges by
reading.

## 7. Settled, and still open

**Settled by the operator, 2026-08-17.**

- **The conversation becomes a real table**, identity only, with `chat_attachment` keyed on it.
  § 6 gives the case that forces it and what it costs.
- **The SPA is a channel like any other, and differs on exactly one axis** (operator, 2026-08-17:
  "the spa should mostly use the same affordances/operations/protocol as the matrix channel
  uses"). Same `ChatFrontend` port, same subscription, same operations, same neutral vocabulary,
  same provenance pointer on the prompts it sends. The one difference is **whether its position is
  durable**, which is the difference § 2 already names — and it is the reason it gets no
  `chat_attachment` row: an attachment exists to hold a cursor, a cursor exists because a channel
  holds a copy the console owes work against, and a tab holds no copy. So "the browser is looking
  at this conversation" is an absence, with no row to key by an address a tab does not have and
  nothing to close when it goes away.

  Read that as narrow. It is a statement about **delivery state**, not a licence for the SPA to
  have its own protocol — which is what it has today (§ 8) and what this whole model exists to
  end. Anything the SPA does that Matrix cannot is either this one axis or a bug.

  **The one sanctioned exception is the debug escape hatch** (operator, 2026-08-17): the SPA may
  show the underlying frames. That is § 11's carve-out already — a debug surface may show the raw
  wire and nothing else may — and it survives the "same protocol" rule because it is addressed
  separately (`/api/conversations/{id}/frames`, never inside the conversation stream), never
  load-bearing, and labelled as one backend's wire. A channel that cannot reach it loses a
  debugging affordance and nothing else.

- **A conversation never ends.** No `ended_at`, no terminal state — it is an id, sessions come and
  go under it, attachments hold and detach. Two consequences: the list surface (§ 9 step 3) needs
  keyset paging from the day it ships, since the list only grows, and "start this room over" is
  detaching the address and attaching it to a new conversation rather than ending the old one,
  which the partial unique index on `(surface, address) where detached_at is null` already permits.
- **A prompt arriving mid-turn is rejected, not held.** The channel says so and the operator
  re-sends; nothing queues behind a running turn. This **reverses R2.5**
  (<../../plans/matrix_chat_runtime.md>), which requires that acceptance not be answering — that
  requirement is superseded here and the cost is accepted deliberately: a message sent mid-turn is
  dropped rather than answered late.

  What it buys is the whole second ingress position: with rejection terminal the watermark advances
  every pass, so `matrix_held_batch`, its backoff, and the `_resolve`/`prompt_fate` mapping of
  `IN_FLIGHT`/`LOST`/`COMPLETED` onto three watermark actions all go.

  **This paragraph used to say `matrix_held_batch` exists to hold an undelivered batch and re-offer
  it. That is backwards, and the correction is the real cost of the ruling** (found building #4291).
  It defers the _acknowledgement_ of a batch that **was** delivered — #4117, and `message_drops.md`
  I3. So deleting it does not only supersede R2.5; it reverses #4117 as well: a prompt accepted by a
  session that dies before claiming it stops being re-offered. It survives as a transcript row the
  replacement is woken with, but it gets no turn until the operator says something else. That is
  pinned by a test today, which is how it was caught.

  **And the loss is wider than "mid-turn".** Admission also refuses on `SESSION_NOT_READY`, so a
  message sent while the sandbox is still provisioning is now dropped rather than delivered when it
  comes up. The notice names the state, so the operator is told to wait rather than left guessing —
  but "a prompt sent to a waking session is lost" is part of what this ruling buys, and it was not
  what the ruling was asked about. Worth revisiting when mid-turn steering or a post-turn queue
  arrives, since both would take it back.

  **One fact still has nowhere to live.** A rejection with no session behind the room cannot be
  recorded at all: a `session_events` row names a session. So it is announced and kept nowhere,
  which is the thing § 4 wants a writer for. It is not dischargeable until an entity above the
  session owns the event — the conversation — which makes it another argument for § 6 rather than a
  loose end of this step.

  **The rejection is an event**, which is what stops this becoming today's `_report_unreadable`
  bug: recorded in `session_events` in the transaction that advances the watermark, with the room
  notice as its projection. Advance-then-announce would lose the message _and_ the notice to one
  crash and tell the operator nothing. Same shape as the abort ruling, and it makes the
  `_report_unreadable` fix and this one the same piece of work.

  **Rejection is the simple answer, not the intended end state** (operator, 2026-08-17). Two
  richer ones are wanted eventually, once the layering is in place, and they are different
  features rather than two spellings of one:
  - **Mid-turn steering.** Claude Code accepts input while a turn is running, so a prompt could
    join the turn in flight rather than be refused. That is a runner-protocol capability, not
    queueing — and the bridge already has an unused input path in that direction (`EndInput` is
    implemented by the runner and called by nothing, § 8's sibling finding), so what is missing is
    a console-side decision rather than a wire.
  - **A post-turn queue.** Hold the prompt and deliver it when the turn ends, which is what
    `matrix_held_batch` does today. If this comes back it belongs **in the conversation, once, for
    every channel** — not as per-channel ingress state. That is why deleting `matrix_held_batch`
    is right even under a future where queueing returns: what is being deleted is one channel's
    private hold, and what would replace it is a conversation-layer queue that the SPA gets for
    free.

  Neither is scheduled. Both want the layering first, because both are about what the conversation
  admits, and admission is a conversation-layer question that is currently answered inside a
  channel's sync loop.

- **Channel state lives in Postgres, not in the room.** The watermark stays a row; `m.fully_read`
  and per-room `account_data` are not pursued. The reason given was preference for the known
  quantity — "postgres is known, state in matrix, who knows" — and it is reinforced by the
  mechanics § 3 already lists: account data is per-user with no compare-and-set while several
  replicas act as one Matrix user, and it is one position where R2.5 needs two. This does **not**
  retire reading the room; § 3's correspondence reader is how the reconciler learns what the room
  currently shows, and that is a read, not a store.
- **One bot serves many rooms, and that is what parallel sessions are** (operator, 2026-08-17).
  This answers the question #4285 raised: `matrix_conversation`'s `user_id` primary key — one room
  per bot user, ever (R3.6a) — was an artifact of the key, not a constraint anyone wanted.
  `chat_attachment`'s partial unique index expresses the rule that is actually wanted: **one live
  conversation per address**, which permits a bot in many rooms at once. Nothing needs to
  re-express R3.6a; it is retired.

  "Only one session holds a conversation at a time" is unchanged and is a **per-conversation**
  rule. What changes is that the console now runs N of them, one per attached room.

  Four things assume the old rule and have to move:
  - **`claim_room` refuses the second room by design** — it binds the first and returns whichever
    is live. It becomes "resolve or create this room's attachment", so an invite mints a
    conversation rather than being turned away.
  - **The supervisor supervises one binding** (`MatrixSessionSupervisor`, "keeps one live chat
    session bound to the one room Haku services"). It becomes a fan-out over live attachments.
  - **The `MXSE` advisory lock is global.** One election that supervises every attachment is
    cheaper than a lock per conversation and keeps the count at one, which § 8 wants anyway; the
    thing that must not be global is the _lease_, and that is already per session.
  - **`MatrixSessionFrontend` takes no address "by construction", citing R3.6a.** With the
    citation gone the port must be bound **per attachment** — see § 5's ruling below, which is
    where the shape is settled, because getting it wrong is easy and #4290 got it wrong.

  **This promotes idle sessions from tidy-up to prerequisite.** One room could afford a sandbox
  held open; ten cannot, and a room nobody is talking to must not hold one. So `create()` stopping
  in `idle` and a prompt being what buys a sandbox (#4231) stops being a nicety and becomes what
  makes many rooms affordable at all.

- **An abort is an event.** It goes into `session_events` the way "this replica took the session"
  does, and the room's "this was aborted" notice is a projection of that row rather than its
  record. Today the abort notice is the one non-reply artifact that is durable, and it is durable
  in the _channel's_ table (`session_outbox`, keyed by `turn_id`) rather than in the record — which
  is exactly the inversion § 5 says the move must fix.

**Still open.**

- **Which notices exist, and what does each summarise?** § 4 proposes one per turn and one per
  session and leaves the set open, along with retire-or-seal for each.
- **Does `sessions.status` survive?** § 10.

## 8. Where this stands

Read against `devel` on 2026-08-17. The three layers exist; the boundaries are drawn in the wrong
places, and the channel is not one thing.

### The channel is three mechanisms with three durabilities

| What                                                                    | Driven by                                                              | Durable?                                                                                                      |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Assistant replies                                                       | `session_outbox` rows, drained by a leader polling at `IDLE_POLL = 1s` | **Yes** — the row is the delivery, redriven after a crash, deduplicated at the homeserver by the row's own id |
| Status line, typing                                                     | `TurnStatus._run`, a 1s poll **inside the turn loop's process**        | No                                                                                                            |
| Lifecycle notices                                                       | `MatrixSessionSupervisor`, woken by `SessionNotifications`             | No                                                                                                            |
| Setup narration, holding, unreadable, silent-turn, room-binding notices | called from the stack frame that noticed                               | No                                                                                                            |

Everything in the "No" rows lives in `RoomPacer._queue`, an in-process deque given `FLUSH_SECONDS`
on a graceful shutdown and nothing on a SIGKILL.

Three consequences, each a thing the reconciler exists to remove:

- **The status line is orphaned by a replica death.** Its `event_id` is a plain instance attribute,
  so the adopting replica cannot edit or redact the line its predecessor posted — it posts a
  second. Typing self-heals only because the homeserver expires it after 30s.
- **A dropped notice is silently gone.** `RoomPacer.send` logs and returns once 200 sends are
  queued. A reply survives that (the row is re-claimed); a notice does not.
- **`_report_unreadable` advances the watermark and queues its notice in-process.** A crash in that
  window acknowledges the message _and_ never tells the operator it was ignored.

### Three leader elections, not one

`MXSY` (the sync loop), `MXOB` (the outbox drain) and the session lease are independent, so three
different replicas can hold them. That is why `RoomPacer`'s token bucket documents itself as an
estimate: two buckets can each believe they own the whole room budget, and only the homeserver's
`retry_after_ms` corrects them. A reconciler per `(channel, conversation)` collapses these to one
holder, which is the point at which a rate budget can be real rather than estimated.

### What is already the right shape

- **Ingress's watermark.** `matrix_sync_watermark` is where the loop reads from. The second
  position beside it (`matrix_held_batch`) was right under R2.5 and is not under § 7's rejection
  ruling — see § 8's own list of what is missing.
- **The ordered address.** `session_events.event_seq`, with `PROMPT_ENQUEUED` now on it, so the
  stream carries both halves of a conversation.
- **The wake with no payload.** `session_changed` names a session and nothing else, coalesced at
  500ms. Built for the SPA; the same wake is what replaces the outbox drain's 1s poll.
- **A position-addressed read.** `GET /api/conversations/{id}/changes?after=` (#4257) is the
  conversation→subscriber half working end to end for one consumer kind.
- **The correspondence key.** `EventTag` rides on every event the console sends. Write-only today.

### What is missing

1. **The conversation has no identity.** Every cross-session read is keyed by `Session.room_id`;
   `matrix_conversation` is one row per bot user, so one room ever, and its `session_id` pointer is
   maintained by the supervisor with no constraint tying it to a session whose `room_id` matches.
2. **Nothing records what we sent.** No table holds a Matrix `event_id`; `post_reply` discards the
   one it gets back. Correspondence must therefore be re-read from the room, or stored.
3. **A prompt does not record where it came from.** The Matrix event ids survive only as the text
   `"[{event_id}] {body}"` inside `session_messages.content` — recoverable by parsing brackets out
   of prose, which is not a join. This is the provenance the operator asked for on 2026-08-17.
4. **Facts that exist only in a stack frame.** The holding count and an unreadable event's msgtypes
   are announced and kept nowhere, so they cannot be projected — only sent.
5. **Egress has no wake.** The outbox drain polls; nothing listens for `session_changed` on the
   channel side.

## 9. The order

**Do not start with the reconciler.** Each step is independently reviewable, and every one is worth
having even if the loop is never built.

1.  **The browser joins a session instead of creating one.** No schema and no dependency on anything
    below, and it is most of what "both surfaces open on one conversation" looks like from the
    operator's side: `enqueue_prompt` has no surface check, so what is missing is a chat page that
    opens an existing session. **Shipped as #4282, on the whole-conversation refetch**, which is the
    correction its author made to this sentence: § 8's increment route is #4257, still open and with
    no frontend consumer, so this step could not and did not build on it. Reading the increment is
    its own later change, and it is invisible to the operator when it lands.
2.  **`conversation`, then `chat_attachment` keyed on it** (§ 6). One schema change, because the
    attachment's key is the whole point of the identity — building the attachment on `session_id`
    first would mean writing the re-pointing logic and then deleting it. Subsumes
    `matrix_conversation` and `sessions.room_id`.

    **Two releases, not one** — the correction #4285 made to this step. Schema, backfill and
    writers ship first (#4285); **a reader keyed on `conversation_id` waits a release**, because
    for the length of the roll the previous image inserts sessions with it NULL and any join
    through it silently omits them. `RoomTranscript.recent` losing a room's re-awakening history is
    the concrete case. Same gate as the `SET NOT NULL`.

    **Two nullability traps sit on the columns this subsumes**, found while writing it and worth
    knowing before step 2's second half is scheduled:
    - `sessions.surface` is `NOT NULL` **with no server default** — precisely `session_frames.partial`
      again. The release that unmaps it omits it from every `INSERT` and Postgres rejects the first
      session of that roll, so it needs `server_default='spa'` in a migration landing _before_ the
      unmapping. Four steps, not three.
    - `sessions.room_id` is nullable and undefaulted, so it is safe alone — but
      `ck_sessions_matrix_room` couples it to `surface`. Drop the CHECK before either column is
      unmapped, and unmap the pair together.

    **Find every reader of `sessions.room_id` before unmapping it, and one is outside `x/`.** The
    audit counted six, including `recall_index_reader` — a consumer in another package entirely,
    which a sweep of the chat runtime would not have turned up. "Subsumes `sessions.room_id`" is a
    statement about the column; the readers are what makes it work.

3.  **The conversation list surface**, keyset-paged from the start, since § 7 settles that a
    conversation never ends and so the list only grows. `/chat` and `/conversations` merge
    (<session_channels.md> § 2) into one list of conversations — each showing its attached channels,
    its last activity, and whether a session is live — with "new conversation" minting the
    conversation and its first session together. Step 1 lands before this and lists sessions; the
    list query is rewritten once here, which is the price of not blocking a visible surface behind a
    migration.
4.  **Reject a mid-turn prompt instead of holding it** (§ 7), and **record both the rejection and
    the unreadable-event notice as events** — one piece of work, because both are "the fact is
    recorded, the notice is its projection", and it is what lets the watermark advance every pass.
    `matrix_held_batch` and its backoff go with it.
5.  **Record a prompt's provenance** (§ 8's missing item 3), **built as #4289**. A structured link
    from the transcript row to the channel events it came from, rather than brackets in prose.
    Smaller than it looked: `PROMPT_ENQUEUED` is already an `AUTHORED` event, so the link is one
    field on `PromptBody` — no table, no migration, no join. Two corrections this step made to its
    own description: **the plan said "the channel event", singular, and a prompt is a batch**, since
    `/sync` folds several room events into one prompt, so the field carries the attachment plus the
    refs; and the union is closed with the SPA named rather than implied by absence (§ 4).
6.  **Move the artifacts that are durable in the wrong layer.** The abort notice is a
    `session_outbox` row keyed by `turn_id`; § 7 settles that it is an event. This is also what gives
    a reconciler the conversation-side identity it needs to be at-least-once, which § 3 names as a
    prerequisite rather than a follow-up.
7.  **Give the facts in stack frames a writer** (§ 8's missing item 4), so a notice body can be a
    pure function of the record.
8.  **Record what we sent** (§ 8's missing item 2), or turn on the correspondence reader. § 7's
    ruling puts channel state in Postgres, which argues for storing the `event_id` beside the
    attachment rather than deriving it from the room every pass; the room read stays available for
    repair. This decides how idempotence works, so it wants its own PR. **Built as #4307, and the
    table it added is narrower than its name** — see § 5 § `chat_delivery` is a revision index: it
    serves channels that hold an addressable, editable copy, and an append-only one is served by
    step 9's cursor instead.
9.  **Matrix becomes a subscriber** (§ 2's primitive): one loop per `(channel, conversation)`,
    reading the record from its cursor instead of being handed events by the turn loop, woken by
    `session_changed` and by inbound events with the 1s poll demoted to a fallback. Here the three
    elections collapse to one, the three egress mechanisms become one difference calculation, the
    pacer's bucket stops being an estimate, and the browser stops being the only consumer that reads
    the record. **`_frontend_for` is deleted here, not improved** (§ 5): the turn loop stops holding
    a channel at all, so the question "which frontend is this session's" stops being asked rather
    than being answered better. Three things go with it, each found by the audit and none of them
    obvious from the symbol: a **green test** pinning the surface-matching contract, which deleting
    the code deletes; **two `x/README.md` sentences** restating the doctrine; and
    `SessionIntroduction.room_id`, **required rather than optional** in a struct the SPA also
    builds. And one correction to make here rather than carry: `x/README.md` claims "the live path
    no longer knows that `assistant`, `stream_event` and `result` exist", while `_run_turn` reads
    `subtype`, `stop_reason` and `result` straight off the payload. The code is honest —
    `_CompletedTurn.frame` calls itself the escape hatch — so the README is the false half, and a
    second backend would degrade silently in both directions until it is fixed.
10. **Notices as spans** (§ 4), once 7 and 9 exist: one work notice per turn, one lifecycle notice
    per session, **each body a fold over the subscription stream**, each retired or sealed. This is
    Matrix's streaming — the granularity a channel that holds a permanent, federated copy can
    afford. The fold is not optional; editing already-posted notices is, and a v0 that appends one
    notice per noticeable event is the accepted lesser form (§ 4). Split the fold from the send
    path in the same PR, because the fold is where this step's tests live.
11. **Delete `ActivityStarted`/`ActivityCompleted` from the vocabulary** (operator, 2026-08-17):
    the `case "task_started"` arm of the projection, both dataclasses, the `session_events` bodies
    that store them, and `room_status.coarse_status`'s arm — so a status line that would have shown
    the harness's prose shows `writing`. That loss is the price of the invariant. **Keep
    `ConversationEventKind.ACTIVITY_*` and `ck_session_events_kind` as they are**: rows of those
    kinds may exist, the column is parsed rather than read as text, and a previous image still
    writes them for the length of the roll.
12. **Delete the rows and narrow the kind** — **unblocked: 11 has converged.** Delete every
    `activity_started` and `activity_completed` row, drop the two enum members, narrow the CHECK:
    one migration, the shape `0059`'s downgrade already uses. Order matters in one direction only —
    deleting the members while their rows survive makes reading one raise rather than degrade. The
    gate was "a release after 11", and the deployed image contains #4279 by ancestry. The same
    reading cleared **15** (gate #4278, done as #4306) and the purge's **phase 3** (gate #4266).
    **And #4285 has since deployed** — the image is `3da90ff`, which _is_ #4285's merge — so step
    2's reader half and step 3 are unblocked too, which they were not an hour earlier.
    Re-derive all of that with § 12's two commands rather than trusting this sentence — it is a
    reading, and it goes stale on its own.

13. **Audit the rest of `ConversationEvent` against the same question** — could a second backend
    produce this, or is it one provider's concept renamed? — **done, and its findings are § 11's**
    rather than a separate note, since what survived is one rule and one open item. It found one
    more failure, `ToolReferences`, deleted along with its whole union by #4284; and it left
    `Projection.unprojected` needing a reader or a deletion, which is the only piece outstanding.

14. **Delete `Usage`** (operator, 2026-08-17), for a different reason from 11: not that it fails
    the neutrality test — it passes, being a reduction to quantities every backend reports — but
    that nobody wants the feature. `Usage` leaves `TurnCompleted`, `_usage` leaves the projection,
    `TurnUsage`/`_turn_usage` leave `session_views`, `ConversationTurnView.usage` leaves the API,
    and the frontend stops rendering cost. Pure code.
15. **Unmap the `session_turns` usage columns** — **done (#4306).** No server default was needed:
    all five are nullable, so it could not hit the `session_frames.partial` trap. That does _not_
    generalise — `sessions.surface` is `NOT NULL` with no default and needs one before its own
    unmapping, which is what makes step 2's sequence four steps rather than three.
    `ck_session_turns_usage_counters` stops being declared and stays in the database until 16 drops
    it with the columns; an insert naming none of the three satisfies it trivially.

16. **Drop them** — **done (#4311).** 15 converged: the deployed image is `3d909fb`, which contains
    #4306. `ck_session_turns_usage_counters` goes with the five columns, as 15 said it would.
    One thing the drop moved that the step list did not anticipate:
    `test_the_backfill_reads_the_payload_the_columns_replace` migrated **to head** and then selected
    the counters, so head passing `0069` left that assertion with nothing to read; it is pinned to
    `0056` instead — the revision its secondary claim was always about, and the last one where the
    counters survive it.

    **`matrix_held_batch`'s tombstone cleared at the same time and is not this PR's.** Its gate is
    #4291, which is merged (`d1640f79df`) and an ancestor of the deployed image, so `DROP TABLE` is
    now the third leg of the same rule. It is independently approvable, so it gets its own PR rather
    than riding along with five columns of a different table.

17. **Many rooms at once** (§ 7's ruling). `claim_room` stops refusing the second room and becomes
    "resolve or create this room's attachment"; the supervisor fans out over live attachments
    instead of supervising one binding; `MatrixSessionFrontend` takes its address rather than
    claiming it has one by construction. Depends on 2's reader half, since "which conversations
    have a live attachment" is the query the fan-out runs. **Do not schedule it before idle
    sessions land** — ten rooms each holding a sandbox is the failure this would ship — **and not
    before step 9 either**, which the dependency line originally omitted. § 7 listed four things
    that assume one bot serves one room; an audit of the code found **eleven**. The seven it adds
    are all per-bot-process state that step 9 deletes: `_status_event_id`, `_status_body`,
    `_holding`, `_last_announced`, the single `RoomPacer` with its one collapsing status slot,
    `_serviced`/`_live_room`, and `RoomOutboxDrain` claiming only `bound_room()`'s rows. **Two are
    already gone**: step 8 (#4307) made the status line read its event id out of `chat_delivery`
    rather than `_status_event_id`/`_status_body`, which was not the minimum that step needed but
    is what keeps the new table from being write-only — and it fixes § 8's orphaned status line,
    where an adopting replica posted a second line beside its predecessor's. Every one
    fails **silently** with a second room — ingress dropped to a `logger.warning`, a status line
    that never appears in room B, an edit addressed at another room's event id, replies that sit
    unsent forever. Shipping 17 before 9 is shipping all seven.
18. **Close R1.2's replay window.** A crash between `enqueue_prompt` committing and the hold being
    written re-delivers a batch the session already has. Found while building step 5 and dropped
    from it deliberately — different query shape, distinct bug, its own review. The trap it left
    behind is worth carrying: **suppression is not acknowledgement.** Skipping the re-delivered
    event and letting the watermark advance trades a duplicate ask for a _lost message_, because
    the session holding the prompt can still die before answering it. The fix re-establishes the
    hold rather than dropping the event.

19. **Give the abort notice a home in the record, and stop concatenating it into prose.** Step 6
    moves the `session_outbox` copy; an audit found two more that § 7 does not name. The notice is
    **appended to `session_messages.content`** on two of `_run_turn`'s three branches, so a row
    whose `source_first_frame_seq` is required to be set now spans prose no frame in its range
    carries, and the row disagrees with its own stored `MessageCompleted` — nothing compares them,
    since `reprojection.check_session` aligns `session_events` against the fold and never looks at
    `session_messages`. On the third branch — a message completed, then aborted before the next
    opened — a session with no channel gets `spoke = False`, `_speak` returns on
    `frontend is None`, and **the notice is written nowhere at all**. That last one is § 5's
    frontend-per-surface structure showing up as data loss rather than as a name, which is why it
    belongs with step 6 rather than after it.

20. **Retire `surface` from the read models, not just from the column.** § 11's table retires
    `sessions.surface` and `.room_id`; the API field and its renderer are named nowhere.
    `ConversationSessionSummary.surface`, `ConversationSessionView.surface`,
    `conversation_records.Conversation.surface` and the SPA's `surfaceLabel` each say a
    conversation has exactly one channel, which is the shape acceptance behaviour 5 cannot render.
    Step 3 rewrites the list surface and is where this is decided — today's field being the obvious
    thing to carry forward is precisely the risk.

21. **The record shapes still call a session a conversation, and let it end.**
    `conversation_records.Conversation` is keyed by `session_id` and carries a terminal status;
    `ConversationSessionSummary`/`View` and the `{session_id}` route say the same. § 6 introduces
    the identity and step 3 rewrites the list, but neither names the MCP record, the two views or
    the route — and the model name reaches the generated MCP schemas and the frontend validators,
    so it is a rename with a blast radius rather than a local one.

22. **Say whose wire the frame inspector shows.** § 11 sanctions it on three conditions and it
    meets two: addressed separately, never load-bearing. It is not labelled as one backend's wire,
    and `read_transcript`'s own instructions claim "one vocabulary that names no agent backend".
    One sentence of labelling, and the carve-out is honest.

Steps 11–16 are a separate lane from 1–10: nothing in the layering depends on them and they do not
wait on it. **Both deletions lose something recoverable rather than something gone.** `Usage` is
read straight off the `result` frame's payload — `usage.input_tokens`, `cache_read_input_tokens`,
`total_cost_usd`, `duration_ms` — and what `ActivityStarted` recorded came off `task_started` the
same way. Both payloads stay in `session_frames`, the surface allowed to be provider-shaped, so
wanting either back is a re-fold over frames. That is what makes these cheap rather than a bet.

Steps 1, 4–7, 11 and 13 are independent of each other and of step 2; 3 and 8 depend on 2; 9 depends
on 7 and 8; 10 depends on 9; 12 depends on 11 converging; 15 on 14, and 16 on 15; 17 on 2's reader
half, on step 9 and on idle sessions; 19 rides with 6, and 20 and 21 with 3, which is
where the shapes they name are rewritten. Step 13 may reorder 11 and 12 by finding more members that fail, which
argues for doing it early rather than waiting.

## 10. `sessions.status` is derived, and lossy

Not a layer question, but it lands on the session layer's own record and came up deciding it
(2026-08-17).

**Derived.** `responding` already is: no path writes it to the column, and `session_view` computes
it from an open `session_turns` row — the SPA switches on the API field, so the column stopped
carrying it with no frontend release. Of the rest, `provisioning`/`ready` follow from
`bridge_connected_at`, and `failed` from `error IS NOT NULL`. Only `closing` and `closed` have no
evidence in the row today, and `idle` has none until the writer lands.

**Lossy, which is the stronger argument.** `provisioning` stands for several distinct facts — claim
submitted, pod scheduled, sandbox running, runner dialled back — and the row records exactly one of
them. So a session that never came up reports `failed` plus free text, where the operator wants
"the claim was never satisfied" told apart from "the sandbox ran and the runner never dialled".

**The shape that replaces it** is the timestamps that actually happened —
`claim_submitted_at`, `sandbox_ready_at`, `bridge_connected_at`, `close_requested_at`, `ended_at` —
with the enum computed in one place and kept as the wire vocabulary. Two things fall out: the
invalid states the current shape permits (`closed` beside an open turn, `ready` beside a non-null
`error`) stop being representable, and `idx_sessions_expired_lease`'s partial predicate stops
listing statuses, so adding a member no longer edits an index.

**Split it.** Adding the terminal timestamps and deriving `closing`/`closed`/`failed` is one change;
dropping the column is the follow-up once every member computes. Both are expand/contract on the
hottest table, and `idle` is being added to the enum right now (#4231), so this waits for that.

## 11. What it looks like when it is done

### The shape

A **conversation** is an id. Under it, sessions run one at a time and end; attachments hold at the
same time and detach. Both point at the conversation; neither points at the other.

```text
conversation ──< session   (serial: one runner each, replaced when the sandbox goes)
             └─< attachment (concurrent: a room, a browser, whatever comes next)
```

Every fact is written once, on the session that produced it, into `session_events` and the
transcript. Every channel reads that record through one subscription (§ 2) and renders it in its own
vocabulary. Nothing is pushed at a channel; a channel is told to look.

### The invariants to hold it to

These are the acceptance criteria, and each names something that is false today:

- **No code outside a channel names a channel's address.** `_enqueue_reply` stops branching on
  `chat.room_id`; the turn writes the record and nothing else.
- **Nothing is announced that is not recorded.** Every notice body is a fold over the subscription
  stream (§ 4), so there is no fact whose only copy is a stack frame, no notice that a SIGKILL can
  silently swallow, and no body a second consumer could not reproduce from the same messages.
- **A replica death costs latency and nothing else.** No orphaned status line, no second status
  line, no acknowledged-but-unreported message.
- **No channel knows a provider's frame shape.** Not Claude's `type` strings, not its content
  blocks, not its `result` envelope. A channel is written against `ConversationEvent` and the
  transcript, and it cannot tell which backend produced them. See below for the one carve-out.
- **Adding a channel is implementing two things** — how to render the stream, and where to keep a
  cursor if it holds a copy. Nothing else in the console changes.
- **Adding a backend is implementing one thing** — an adapter producing `ConversationEvent`.
- **Every event derived from a provider's frames names the frames it came from** (operator,
  2026-08-17). What it buys is that a normalization can always be appealed to the raw JSON, which
  is what makes the vocabulary's lossiness — a tool result rendered as a string, `Usage` deleted,
  the activity events deleted — recoverable rather than final.

  **This is a convention, not an enforced invariant, and an earlier draft of this line said
  otherwise.** What is enforced is the union's internal consistency: `FrameRange | Authored` in the
  type, and `ck_session_events_provenance_frames` saying frames are present on exactly the
  `frame_range` arm. What is _not_ enforced is that a frame-derived **kind** must take that arm.
  `ConversationEvent.provenance` is the same union on every member, so a `MessageCompleted`
  carrying `Authored` type-checks; no CHECK ties a `ConversationEventKind` to a provenance, so the
  row it produces passes every constraint; and `session_events.row` writes it as `authored` rather
  than raising. Nothing reaches that state today only because the Claude adapter always sets a
  range.

  **It breaks asymmetrically, which is why it is worth a constraint rather than a docstring**: a
  second adapter that forgot would write silently and fail on _read_ — `session_views._asked`
  raises on a tool-call row with no frame, and it runs on every `SessionStore.get`, so one such row
  makes a whole session's transcript unreadable. The gap sits exactly on the seam a second backend
  arrives through, and this list previously recorded it as done, which is the more dangerous of the
  two errors. Closing it is a CHECK tying the frame-derived kinds to `frame_range`, and a
  `session_events.row` that raises instead of downgrading.

  `session_messages` and `session_turns` carry the same span, and there
  `ck_session_messages_assistant_pointed` does make an unpointed assistant row unrepresentable —
  the shape `session_events` still wants.

The last two are what "N backends × M channels, additively" actually means, and they are the reason
for the whole exercise.

### The one thing a channel may know about a backend

**A debug surface may show the raw wire, and nothing else may.** The frame inspector already is
this: `/api/conversations/{session_id}/frames` serves `SessionFrameView.payload` as
`dict[str, Any]` — the frame whole, deliberately unclipped, because `session_messages` is a lossy
projection of it and clipping there would reintroduce the loss one level down.

Three conditions keep that from becoming a hole:

- **It is addressed separately.** A provider payload never rides inside the conversation stream, so
  a channel cannot consume one by accident — it has to go and ask a different route for it.
- **It is never load-bearing.** No rendering decision, no notice body, no delivery decision reads
  it. A channel that cannot reach the inspector must lose nothing but a debugging affordance.
- **It is labelled as one backend's wire**, not as the conversation. A reader of that surface is
  looking at Claude's frames and should be told so.

**The neutral vocabulary is not audited against this rule, and at least one member fails it.**
`ActivityStarted`/`ActivityCompleted` are produced by exactly one arm of `x/claude_code/projection.py`
— `case "task_started"`, reading `task_id`, `tool_use_id` and `description`, which are Claude's own
field names — and the dataclass docstring says as much: "the harness's own prose". A channel
rendering one is knowing a provider's concept at one remove, which is the rule broken by a member
of the vocabulary rather than by a channel. Retiring it is § 9's steps 10 and 11; what it records is
recoverable from `session_frames`, which is the surface allowed to be provider-shaped.

**The rest has been asked, and the answer is in.** Every member was read against its producing
`case` in the adapter and the payload keys that arm reads. `TextDelta`, `MessageCompleted`,
`Reasoning`, `ToolCallStarted`/`Completed`, `TurnCompleted`, `Outcome`, `MessageKey` and the
provenance types are general. Two failed and are gone: `ToolReferences`, which turned out to be not
one provider's `tool_result` shape but **one tool's result shape on one provider** — every one of
its 51 sightings is Claude Code's deferred-tool search — and with it the whole
`ToolResultContent` union, since #4284 made a tool result's `content` a plain string.

**The rule the audit produced is the part worth keeping**, because it is what decided the cases that
looked alike: **the line is not how Claude-shaped a thing is, but where the shape lives in the
type.** `ToolCallCompleted.structured` is _also_ one tool's shape and it stays, because it sits
behind `Json` — a per-tool payload is sanctioned (R6.3 passes tool identifiers verbatim; a channel
rendering a Bash result's `stdout` knows Bash, not Claude) while a per-tool shape promoted to a
typed member is not. That bounds the leak surface exactly: `Json` marks three fields, and
`Projection.unprojected`'s keys are a fourth the type does not mark.

**`unprojected` is the one thing left, and it is not the leak it looks like.** Its keys are Claude's
own frame class names (`system/vcs_state_changed`, `user/text`), but it is produced by the adapter —
the one component allowed to be provider-shaped, since translating is its job — so it reaches no
channel and breaks no invariant.

**An earlier draft of this paragraph said no production code reads it. That was wrong**, and the
way it was wrong is worth keeping: the field is _renamed_ downstream, so grepping the name finds
only tests. `transcript_entries.unreadable` folds it, `SessionStore.read_transcript` carries it as
`TranscriptSlice.unreadable`, and the MCP `read_transcript` tool serves it as
`TranscriptPage.unreadable`. Deleting it would have removed a live wire field an agent already
receives, not dead payload. **A name-based search does not establish that a field has no reader
when a projection renames it at a layer boundary.**

What was actually missing was a reader **a human** reaches, and #4303 gives it one: each row of the
frame inspector carries its own `unprojected` and the SPA badges it. That satisfies § 11's
carve-out rather than widening it — addressed separately, never load-bearing, labelled as one
backend's wire (#4305) — and it costs no storage, since the page's own payloads are folded on
read. Two other members were noticed in passing and neither is leakage: `Authored` is
constructed nowhere outside a test, a shape waiting for the writer step 4 adds; and `Outcome`'s
premise was wrong in the asking — `result.subtype` never reaches it, it is fed from `is_error` and
`status`.

**What enforces the rule does not exist yet.** `x/frame_projection.py` imports
`x/claude_code/projection` directly, so there is no seam at which a second backend's adapter could
be selected — the fold is spelled in terms of one provider by construction
(<../../runtime/x/bridge/docs/second_backend.md>). Until `CliBackend` grows that member, the
invariant is a convention held by review rather than by the type system, which is the same shape of
gap that let the turn loop become a frame interpreter in the first place.

### What disappears

**Tables and columns**

| Gone                                                              | Replaced by                                                                                                              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `matrix_conversation` (all four columns)                          | `chat_attachment`, whose partial unique index is what "one session holds it" means                                       |
| `sessions.room_id`, `sessions.surface`, `ck_sessions_matrix_room` | the attachment (<../../plans/chat_runtime_cleanup.md> § stage 7 already says so)                                         |
| `session_outbox`                                                  | the reconciler derives what it owes by comparing the record to the room; a queue that holds no facts need not be durable |
| `sessions.status`                                                 | the timestamps of § 10, with the enum computed                                                                           |

`matrix_held_batch` also goes, with the hold semantics it exists for (§ 7). Not gone, and
deliberately: `matrix_sync_watermark`, `session_events`, `session_frames`, the lease.

**Mechanisms**

- `RoomOutboxDrain` and its `MXOB` advisory lock — one election instead of three.
- `TurnStatus._run`'s in-process poll, and the turn loop calling `show_status`/`set_typing` at all.
- Every per-process latch that stands in for durable state: `_status_event_id`, `_status_body`,
  `_holding`, `_last_announced`.
- `RoomPacer` as a queue of opaque callables — a budget the reconciler spends, not a deque of
  closures it cannot inspect or squash.
- `claude_chat_page.tsx` and `/api/sessions/{session_id}/stream`, at step 3.
- `RoomTranscript.recent`'s join on `Session.room_id` — the tail handed to a replacement session
  becomes a conversation read, which is what it always meant.
- Parsing `[event_id]` out of prompt text to learn what a prompt answered.

### How to tell it is finished

Six behaviours. The first four are the surfaces working; the last two are what proves the
architecture rather than the features, and each is the acceptance test for the thing that forced it.

1. **The Matrix surface still works.** `x/channels/matrix/test_fullstack_e2e.py` against real
   Synapse stays green throughout. A regression gate, not a new test — if it goes red, something
   in the layering broke the channel that already worked.
2. **The web surface works**: one merged surface lists conversations, creates one, sends, aborts,
   and shows a transcript.
3. **The web surface streams.** A message being written grows in place, without a whole-transcript
   refetch. Per token is what § 2 designs and what this test should assert once the delta kind
   exists; a v0 that only carries the open row at `COALESCE_WINDOW` granularity passes the weaker
   form of it, and the follow-up tightens the assertion.
4. **Both surfaces show the operator's own prompts, wherever they were sent from** (§ 4). A prompt
   typed in the SPA appears in the room; one sent from the room appears in the tab; neither appears
   twice on the surface it was typed on. The first case does not work at all today, and the third is
   what the provenance pointer exists to make true. **A prompt whose origin predates the pointer is
   shown where it already is and nowhere else** — the conservative answer, since the alternative
   re-posts history into a room.
5. **One conversation, two surfaces.** A room and a browser both open, either can prompt, both
   show the same account. This is what the conversation entity is for, so it is its test.
6. **A session replacement is invisible to both.** The sandbox is killed mid-turn, a new session
   takes over, and both surfaces show the restart without either being told twice. This is the
   case that forced the entity (§ 6), and a replica killed mid-turn leaves nothing orphaned or
   duplicated in the room.

**Write these per step, not at the end.** A failing test cannot land on `devel`, so each step's PR
carries the test that proves its own part, and 5 and 6 land as end-to-end tests with the steps that
make them passable — 3 and 9. Behaviour 4 lands with step 9, which is where a channel starts reading
the record rather than being handed the half the turn loop produced. Saving them for the end means running blind until then, which is the
particular difficulty of working this unattended.

**Encode § 11's invariants as tests too**, because each is false today and would otherwise creep
back silently:

- `session_store` contains no reference to `room_id` — one structural test, and it is the whole of
  "no code outside a channel names a channel's address".
- **Notice bodies are folds**, tested as folds: a list of `ConversationEvent`s in, a body out, no
  room and no database. The cases worth writing are the ones an end-to-end test cannot provoke on
  demand — a forty-call run collapsing to a tally, a session replaced mid-turn, a turn aborting
  between two tool results.
- The Matrix channel's Bazel target cannot depend on `//haku/console/x/claude_code:*`. Restricting
  that package's `default_visibility` to `//haku/console/x:__pkg__` enforces "no channel knows a
  provider's frame shape" at build time in one line. The one exception it surfaces —
  `x/channels/matrix/testing` depending on `stub_claude_bin` and `:frames` to drive a fake backend
  — is worth having to write down, because a channel _test_ knowing Claude's frames is how a
  channel comes to know them.

## 12. How this executes

§ 9 is the dependency order. This is how it is worked: what fans out, what cannot, and where the
position is kept so a session that dies mid-flight loses nothing but its own context.

### The bottleneck is migrations, and it is narrower than the step list suggests

**At most one migration-bearing PR open at a time, or stacked.** Two agents independently declared
`revision = "0062"` off `0061` within an hour of each other — a duplicate id _and_ a fork, both
invisible to `git merge-tree`, both stopping the console booting. With N agents that is the default
outcome, not bad luck. Stacking is fine (declare the parent's revision and say so in the body);
racing is not.

**But four steps needing a migration is not four migrations.** Steps 4, 5 and 6 all widen the same
`ck_session_events_kind` or add one column beside it, so they collapse into one permissive schema
change. That matters because of what it unlocks:

> **Land the permissive schema first, then fan out the writers.** A widened CHECK forbids nothing
> the previous image writes and has no reader, so it is safe alone and can merge on its own. Once
> it has, the rejection writer, the abort writer and the provenance writer are three independent
> pure-code PRs that can be written at once.

That turns the longest serial stretch in § 9 into two migrations and a wide fan.

### The lanes

| Lane          | Steps                                  | Shape                                                                                                           |
| ------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Schema**    | 2, then the kinds+provenance migration | Serial. One open at a time or stacked. Everything else waits on lane head only where it truly needs the column. |
| **Surfaces**  | 1, 3                                   | Parallel. 3 needs lane Schema's first migration; 1 needs nothing.                                               |
| **Writers**   | 4, 5, 6, 7                             | Parallel behind the kinds migration. Each is one fact recorded where it belongs.                                |
| **Reduction** | 11→12, 13, 15→16                       | Parallel with everything. Gated within itself on release convergence, not on other lanes.                       |
| **The chain** | 8, then 9, then 10                     | Genuinely serial and last. 9 is the reconciler; 10 is what it makes possible.                                   |

### What every agent is told

- **Never merge.** The operator merges. Say plainly when a merge has an ordering constraint.
- **Amend this plan in your own PR** when implementation shows it is wrong, and say so in the body.
  It has been wrong four times: `partial`'s missing server default, the index that needed its own
  exclusion, phase 2 never having landed, and `task_notification` producing an activity event.
- **Abort and report** rather than deciding anything architecture-level — what a session or
  conversation _is_, a new table, or a decision that binds other steps.
- **Collisions are expected**, and coordination is not the fix. Whoever lands second rebases, and
  a conflict between two deletions resolves to _both_ deletions — taking one side wholesale
  silently reverts the other and CI goes green anyway, which is the failure mode to watch for.

### Where the position lives

This document. A session running this loses its context; the plan, the rulings and the order are
what survive, which is the argument for landing it early rather than holding it until the work is
done. Each PR that completes a step deletes that step here, so what remains is the work that
remains.

### Checking the world instead of remembering it

Three habits that are not obvious from the step list, each written down because forgetting one cost
something today.

**Deploy gates progress on their own. Re-derive them; never carry them.** Several steps are gated on
"the release that stopped writing X has converged", and that becomes true without anyone doing
anything — so a gate checked an hour ago is not a fact, it is a stale reading. I carried
`654a4fc` as the deployed commit while the cluster had rolled to `351dd27`, and spent that time
describing three steps as blocked that were not. The check is two commands and there is no excuse
for inferring it:

```bash
kubectl get pods -n haku-console -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}'
git merge-base --is-ancestor <merge-commit> <deployed-commit> && echo converged
```

Read the commit suffix off the image tag and resolve it by **ancestry**, not by comparing tag
timestamps or trusting a PR title. Both replicas must report one tag: mid-roll, the older pod is
still serving.

**Production is queryable, so check the data before scheduling work against it.** The console's MCP
server answers over the network with the bearer in the `haku-console-agent-api` secret in the
agent's own namespace — `kubectl get secret haku-console-agent-api -o jsonpath='{.data.token}'`,
base64-decoded, against `https://haku.allegedly.works/mcp`. Its `haku_conversations__*` tools read
production sessions, transcripts and frames, which is how a question like "are there rows of this
kind left to delete" gets an answer rather than an estimate. `hostexec__bash` is approval-gated and
pages the operator, so it is for when a shell is genuinely required and not before.

**Fan out hard, then babysit what you dispatched.** Agents work in their own worktrees and PRs go
out in parallel; the operator merges. The half that is easy to drop is the second one — a PR is not
delivered when it is opened, it is delivered when it is green, rebased and still mergeable, and
`devel` moves under all of them. That means a standing sweep rather than a reaction to being told:
merge-cleanliness with `git merge-tree --write-tree` and its **exit code**, every PR's checks with
`perPage: 100` because `Pre-commit checks` lands on a later page, and the migration branches
simulated **against each other** rather than only against `devel`. Prune finished worktrees as you
go; the disk allowance is fixed and agents fill it.
