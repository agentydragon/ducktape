# The chat runtime: sessions, conversations, channels

The model, set by the operator on 2026-08-17:

> LLM session ↔ abstract conversation ↔ messaging channel

with the cardinality settled in the same breath:

> - **session**: runs as long as one runner
> - **conversation**: can span over multiple sessions; only one session holds it at a time
> - **matrix room**: I think we can assume that 1 matrix room == 1 conversation

This is the chat runtime's one plan: the invariant the layering is aiming at (§ 1), the protocols
that follow from it, and § 9's ordered list of what is left to build. Design that is still true is
stated in the present tense; work that has landed is deleted rather than ticked off, so what remains
here is what remains to do.

**Nothing in code cites this file, and nothing should.** A plan empties out as its work lands, so
every identifier in it is transient by construction and a comment pointing at one goes stale
without anything noticing. How the layers work once this plan is spent is <../docs/chat_layers.md>,
a contract several call sites depend on belongs in <../x/README.md> or a `SPEC.md`
(<../x/channels/matrix/SPEC.md> is where the Matrix channel's own guarantees live), and an invariant
one call site depends on belongs in that call site's own words.

## 1. The invariant this plan exists to reach

The layering is two forbidden edges (operator, 2026-08-18):

> A **channel** listens to and sends to the **conversation**, never to a session.
>
> A **session** listens to and sends to the **conversation**, never to a channel.

The conversation is the only thing either side talks to. **How the three layers work when this
holds is <../docs/chat_layers.md>** — what each owns, the two routes a fact takes into the record,
and where a new table, port or event kind belongs. Every step below is checked against it, and the
rule leaves this plan with the last step that achieves it.

**The neutral log is already the conversation layer in everything but its key.** `session_events`
carries `provenance`, its frame pointers are NULL on the authored arm, and `AuthoredEventKind`
decides membership by _"whether it reached the console over the wire"_ — explicitly not by whether
the fact is about a session. What is wrong is `session_id NOT NULL` and a name that says session.
Step 1.

### The edges that are bugs

Every line here is an edge the invariant forbids, in the code today.

**A session that knows a channel.**

- `ChatFrontend`, and `SessionService._frontend_for` selecting one for a session (§ 5).
- `SessionIntroduction.room_id`: a session's own system prompt is rendered from a room's address.
- `_enqueue_reply` (<../x/session_store.py>) reads the conversation's live Matrix attachment and
  writes `session_outbox.room_id`, inside the turn loop's own transaction (§ 5).
- `MatrixSurface`, reached from `_run_turn`: `report`, `report_silent_turn`, `show_status`,
  `set_typing`.

**A channel that knows a session.**

- `MatrixSessionSupervisor` creates, replaces and tends sessions — `SessionService.create`,
  `expire_stale_leases`, `reconcile_terminal_claims`. A channel owning session lifecycle is the
  deepest of these, and step 2 is what removes the need for it.
- `RoomOutboxDrain` drains `session_outbox`, and the room's status line comes from the open
  `session_turns` row.
- `MatrixConversationStore.recent_history` reads `session_messages` joined on `sessions` and
  filtered by "not this session"; `IngressLedger.unanswered` joins the same three tables.
- `MatrixSyncService.advance` takes `SessionEvent` rows, and every channel read of the neutral log
  is session-keyed for as long as the table is.
- `EventTag.session_id` rides on every event the console posts, so a room's permanent, federated
  copy is addressed by a runner incarnation that a replacement invalidates.
- **The wake itself is session-keyed.** `pg_notify` carries `{kind, session_id}`, so a subscriber to
  a conversation subscribes by session.
- The SPA is a channel too: it posts to `POST /api/sessions/{session_id}/messages`, reads
  `/api/sessions/{session_id}/provisioning`, and takes its setup narration from a query over
  `session_frames`.

Two per-process latches sit beside these and are the same failure in another costume:
`MatrixSyncService._status_body` and `MatrixSessionSupervisor._last_announced` stand in for state
that has no row, so a leader handover re-announces. § 3.

### Why those edges cannot simply be deleted

Four facts the record does not hold, each the reason one push above still exists:

- **Turn lifecycle.** `ConversationEventKind` says it outright: a `TurnCompleted` _is_ the
  `session_turns` row. Nothing records a turn starting, so a room's status line has nothing to
  project from and reads the row instead.
- **Provisioning.** "A session is being provisioned for this conversation" is stored nowhere, so the
  only account of it is the supervisor's stack frame.
- **Setup narration.** `SessionStore.narrate` writes a `setup_output` row into `session_frames` —
  the console minting a wire frame for a fact that crossed no wire — and both surfaces read it back
  out by kind.
- **Room binding.** "Joined — this is now Haku's room", "invited elsewhere, still serving this one":
  queued notices with nothing behind them.

Deleting the push would delete the fact. Recording them first is step 1.

## 2. The protocols in detail

### Session ↔ conversation

**Session → conversation** is the fold: `apply_frame` writes the message row, its `session_events`
rows and `sessions.projected_frame_seq` in one transaction. One writer at a time, because the lease
is single — which is what makes `event_seq` monotone per session, the premise the increment design
depends on.

**Conversation → session** is admission and re-awakening. `enqueue_prompt` accepts a prompt on a
ready session with nothing queued and refuses otherwise; a replacement session is handed the
conversation's tail as prompt text.

**Handover** is the operator's "only one session holds it at a time", and the lease is what holds
it: `authenticate_bridge` admits one runner connection while a lease is valid, and expiry makes the
row adoptable (§ 15). `chat_attachment`'s partial unique index is a different rule wearing the same
words — **one live conversation per _address_** — and reading it as the handover rule is what makes
a channel's row look like a statement about sessions (§ 7).

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

The primitive is code: `Subscription.read()` over a `ConversationStream` in `x/subscription.py`,
where "no position given" is the `Unstarted` arm rather than a zero, and
`WS /api/conversations/{id}/follow` is the same contract over one socket — a snapshot, then the
changes, with `session_changed` as the wake that says to read again. Matrix is a second consumer for
**one** event kind — `RoomNotices` reads `turn_aborted` from the record instead of the turn loop
pushing it. What is missing is the rest of the kinds.

**The stream carries two kinds of message, and a delta is one of them** (operator, 2026-08-17).
State changes are whole rows, merged by id. A **delta** is `{message_id, append}` — the neutral
`TextDelta` the fold already produces and never stores — and every consumer receives it: a room
ignores it because it cannot edit per token, a tab applies it and shows prose as the model writes
it. One mechanism with two message kinds, not a side channel.

**The row is the resync, which is what makes it safe.** `session_messages.content` is mutated in
place per delta, so a subscriber that misses deltas, joins mid-message or reconnects reads the
message row and is correct. No exactly-once, no ordering guarantee, no gap detection — the same
property that made whole-row replacement the right payload for state changes.

**Deltas are never stored.** The completed message carries the prose whole; a stored delta would
double the transcript for no reader. Note what this changes about pacing: `COALESCE_WINDOW` stops
being the rate prose arrives at and bounds only the state messages.

**v0 may ship without the delta kind** (operator, 2026-08-17). The increment already carries the
open message row whose `content` is the prose so far, so a subscriber that ignores deltas still
watches a message grow — at `COALESCE_WINDOW` granularity rather than per token. That is the
fallback if the delta kind costs more than it looks like it will. Adding the kind later changes no
schema and no stored row — only what the socket emits between coalesced increments — so v0 shipping
without it costs a follow-up, not a redesign.

**Conversation → subscriber.**

- **The address is `session_events.event_seq`.** It is a global `Identity` sequence, so one
  session's rows are not contiguous: every read is "everything after N", never "the next one after
  N", and a gap is undetectable by construction.
- **The wake carries no payload**, and names the wrong layer. `session_changed` names a session and
  nothing else, so a channel subscribing to a conversation subscribes by session (§ 1); it carries
  the conversation from step 1. Level-triggered, edge-scheduled — <../x/session_live_updates.py>
  builds this half: `LISTEN`/`NOTIFY` is broadcast, each replica fans out to the sockets it holds, and changes
  coalesce to at most one per session per half-second.
- **The position belongs to whoever needs it, and its shape follows from what they hold.** A tab
  holds no copy that outlives it, so its position is a query parameter and the server keeps
  nothing. A room holds its own copy, so its position is a durable cursor: a position behind the
  record is work the console still owes. The test that separates them is "does this subscriber hold
  a copy?".
- **The address does not cover an open message, and deltas are why that is survivable.** A
  `TextDelta` is deliberately not a row, so a message being written is invisible in the log until
  `message_completed` while `session_messages.content` is mutated in place. The delta kind rides
  the socket outside the address, which is exactly why it needs no gap detection: whoever misses
  one reads the row.

**Channel → conversation.** A channel whose transport retains unacknowledged input needs a read
position and an acknowledgement position, and the second is a promise rather than a cursor:
`matrix_ingress_event` holds the events a prompt in the record carries, written in that prompt's
own transaction. What is still a poll is the sweep for a prompt nothing answered; under
reconciliation that is the same `session_changed` wake the outbound half uses.

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
`kind`, `session_id`, `message_id` and `agent_message_id`. **The reconciler is its reader**:
correspondence between the record and the room is exactly what step 2 of the loop needs, and the
tag is already the correspondence.

**`session_id` comes off it first.** A room event is permanent and federated, so a tag is the
longest-lived thing a channel stores, and a runner incarnation the shortest-lived thing it could
name: replace the sandbox and every tag in the room names something gone. What a correspondence
reader needs is the conversation and the row, which is what the other three fields already are.

- **Two readers of one `/sync`, with opposite filters.** Ingress excludes Haku's own sender, and
  `MatrixClient._read` drops those events before anything else sees them. The correspondence reader
  is the mirror — only our sender, parse the tag, never a prompt. That constrains what may become
  input, not what may be read, so this is a new path at the client rather than a change of policy.
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
  live line" is self-correcting on the next pass only if the reconciler may redact its own
  duplicate.
- **The token cannot live in the room**, because it is what reads the room. So the channel keeps a
  private store whatever else moves; the only question is what else is in it.

**A prerequisite, not a follow-up: one outbound artifact still has no conversation-side identity.**
`EventTag.transaction_id()` derives from `message_id` where there is one and mints a fresh `uuid4()`
otherwise, so a resend of anything without a transcript row posts a second event. The remaining case
is a turn's final text that no completed message queued, which is why `PendingReply.transaction_id`
uses the outbox row's own id and `uq_session_outbox_turn` keys it per turn. A reconciler is
at-least-once by nature, so that identity has to move into the record before the reconciler exists.

## 4. Projecting tool calls, thinking and session events into a room

A room takes prose. Everything else a turn does — a run of tool calls, a stretch of thinking, a
session being provisioned and replaced — reaches it as a **notice that updates as things happen**.
Naming its parts separately is what makes the projection reconcilable:

- **Its subject** — the span of the conversation it summarises: a turn, a tool-call run, a
  session's life.
- **Its body** — a fold over the subscription stream. Purity is what makes squashing correct:
  whatever the room last received, the next send is recomputed from what has arrived.
- **Its lifecycle** — created when the span first has something worth saying, edited while the span
  is open, and closed when the span is.

### What streaming projects into

The conversation moves per delta; the room moves per message. Between two messages, what the room
can be told is that a turn is live (the typing notice, sent directly rather than through the pacer,
so it spends no queued slot) and how much has happened (the notice). When `MessageCompleted` lands,
the finished message is forwarded whole.

**A message being written cannot be edited into the room**, and the layer argument settles it rather
than the rate budget: the conversation exposes no address for an open message.
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
  a session replaced mid-turn, a turn that aborts between two tool results) become table rows.
- **It is what makes reconciliation cheap.** An at-least-once reconciler needs to answer "does the
  room's copy match what it should show", and a fold gives that answer by construction: re-fold
  from the cursor, compare with the tag's last body, send only if they differ.
- **It forbids the shortcut that would rot.** A body computed from whatever the turn loop happened
  to be holding is a body no other consumer can reproduce — the exact coupling that lets a fact
  reach a room and never reach a tab (§ 8).

**v0 may emit one notice per noticeable event and never edit** (operator, 2026-08-17). That is a
lesser feature, not a different architecture: the fold still runs, the accumulator is still the
carry, and what changes is only that the channel appends its output instead of editing the open
notice's tag. Editing is added later by giving the fold's output a tag to reconcile against, which
touches the send path and nothing about the reduction.

### What a notice body may do

- **Read the neutral vocabulary, never a backend's frames.** `coarse_status` (<../x/room_status.py>)
  reads `ConversationEvent`; a notice that reached into Claude frames would weld the channel layer
  to one backend.
- **Stay bounded independent of the span's length.** Forty tool calls summarise to a tally and the
  one in flight, not to forty lines: a room event is permanent and federated, and an edit
  re-publishes its whole body.
- **Stay coarse by rule.** Where a tool is named, its identifier passes through verbatim; no
  per-tool copy, no mapping table.
- **Say that thinking happened, at most with its summary.** `Reasoning.summary` is the only
  renderable part, and a notice per `Reasoning` event is what the single line exists to avoid.

### A candidate shape, with today's status line as its degenerate case

**One work notice per turn.** While a turn is open the room shows at most one notice: what is
happening now, plus a bounded tally of what has happened already. Today's status line is this with
an empty tally, which makes the change additive rather than a replacement of built behaviour.

**One notice per session for lifecycle.** Today each transition is its own `m.notice`, deduplicated
by a per-process `_last_announced` and dropped entirely before a room is bound. The facts belong to
the conversation as `session_events` rows; the room's rendering of them is the channel's own
decision, and once the timeline is in the record, collapsing the room's copy into one edited line
costs nothing that is not recoverable elsewhere. Under correspondence the live notice's tag is the
dedup state, so a leader handover stops re-announcing.

**Retire or seal is the open half of this.** A notice whose subject is live state should be
redacted when the state is gone (a spent status line is clutter). A notice whose subject is a fact
that happened should be sealed with a final edit instead — retiring it loses the account of what the
agent did while working. Which notices exist, and which of the two each is, is left open (§ 7).

### The operator's own prompts are part of what a surface shows

**A prompt is a conversation fact, so every attached surface shows it** (operator, 2026-08-17) —
"send a prompt from the SPA and I'd expect the bot to deliver it to the Matrix conversation too".

It follows from the subscription rather than being a feature bolted onto it. `PROMPT_ENQUEUED` is
already an authored event on the conversation, so a channel reading the record sees the operator's
half as well as the agent's. What is new is only that a channel must **project a prompt it did not
receive** — today the room shows the operator's messages because they were typed there, not because
anything projected them.

**The provenance pointer is what stops the echo, and that is its reader.** A prompt that arrived
through this room is already in this room; re-posting it would duplicate it. A prompt from the SPA,
or from another room, is not — so it must be posted. `PromptBody.origin` carries that answer on
every prompt already; what is missing is a channel that asks it.

**Rendering it is the channel's own decision**: a room may want a prompt from elsewhere marked as
such, since an operator reading the room sees a message they did not send there.

### What a notice does not need

**No outbox row.** Its durable form is the span it summarises plus the room's own tagged copy, so
nothing about it needs a second write on the send path. That is what keeps the rule intact — record
what happened, derive what is shown — and it is why the delivery queue can be channel-private
(§ 5): under reconciliation the queue holds no facts, only work derivable from the record.

The exception is a notice whose fact is not in the record at all. <../debug/channel_write_audit.md>
is the standing inventory of those; anything still on it needs a conversation-side writer before it
can be projected rather than sent.

## 5. Where delivery state belongs

**The outbox goes in the Matrix channel, as its private implementation detail.** `SessionOutbox`
sits in the shared schema (<../database_schema.py>) with a `room_id` column and a docstring
anticipating "a discriminator beside it" for a second channel. Under the layer model there is no
second channel's rows to hold: a channel that keeps no copy (the SPA) needs no queue at all, and a
channel whose transport has no idempotency key needs a different one.

**Moving it is the reconciler, not a rename.** The row is written today inside the turn loop's
transaction, so while it exists the conversation's writer looks up the room's address. Under
reconciliation the turn writes only the record and the channel derives what it owes, which is what
removes that branch. What stays shared is the record and the subscription interface.

**And it is re-keyed on the way in, not merely renamed.** `session_outbox.session_id` and its
`turn_id` are session-layer columns, and `chat_delivery`'s subjects mint `turn:{turn_id}` from one —
so a table that lands inside the Matrix channel still carrying either arrives already breaking the
invariant. A channel's own tables name the conversation and the transcript row.

**And the table should say Matrix** (operator, 2026-08-17: _"the matrix outbox should as matrix impl
be named after matrix in its table and not try to be generic"_). `session_outbox`'s docstring says
the opposite today — "a second one joins by adding a discriminator beside it rather than by
overloading this column" — which is an instruction to the next editor to make it generic, and under
the layer model that instruction is wrong. A second channel does not join this table. It either
keeps no copy and needs no queue, or it has its own with its own retry semantics; a shared
discriminator would put two channels' failure domains in one queue.

Two halves, on different clocks. **The doctrine is wrong now and costs nothing to fix**: that
docstring, and <../x/README.md> § Matrix chat surface's claim that `session_outbox` is neutral and
that record-then-drain is "the shape every outbound channel write has to take". The split is right;
the neutral half is the record. **The rename itself rides with the move**, because `session_outbox`
→ `matrix_outbox` is not a one-step migration under `maxUnavailable: 0` — an old replica selects the
old name for the length of a roll, so a bare `ALTER TABLE … RENAME` breaks it, and the
expand/contract dance is not worth paying twice for a table the reconciler is about to restructure
anyway.

### The cursor and the outbox answer to different layers

They are easy to confuse because both are "what has gone out", so name whose contract each one is
(operator, 2026-08-17):

- **The cursor is how far the conversation has been handed to the channel implementation and
  acked by it.** It is a position in the conversation's own stream — the one thing every channel
  has, including a channel that keeps no copy. What sits on the boundary between the layers is the
  **interface**: `Cursor.position()` / `keep()`. The position itself sits wherever that subscriber's
  durability requires — a tab passes it as an argument to the next read and stores nothing, a room
  keeps it in `matrix_room_cursor`, which is the Matrix channel's own table and nobody else's.
- **The outbox is the channel's queue against the homeserver.** It lives entirely inside the Matrix
  implementation, below that boundary, and what it holds is retry state about a flaky external
  server: `attempts`, `next_attempt_at`, `last_error`, a row that stays unsent until it is not.

**A cursor cannot absorb the outbox**, and this is why both survive. A position says "everything
before here is done", which is only true when delivery is ordered and gapless; a homeserver that
refuses one reply and accepts the next breaks that, and expressing "this one failed three times and
is backing off" in a cursor is just re-deriving a queue. Equally the outbox cannot absorb the
cursor: it is the channel's private business, and the conversation layer must not have to read a
Matrix queue to know how far Matrix has got.

### `chat_delivery` is a revision index, and most of it is write-only

`chat_delivery` (`0067`) is shared by every channel that attaches, and its shape assumes more about
a channel than the layer model lets us assume (operator, 2026-08-17, on a channel that is _"a
teletype printing everything as it happens"_, _"an old timey pager"_, or _"a telegraph key that
sends a constant message when pressed and doesn't display anything"_).

Three of its properties are Matrix's model, not a channel's:

- **`sent_ref` is `NOT NULL`** with a non-empty CHECK, so a channel with nothing to point at cannot
  record that it delivered anything at all.
- **`uq_chat_delivery_live_subject`** — one live row per `(attachment, subject)` — encodes _at most
  one artifact per subject, revised in place_. An append-only channel's correct behaviour when a
  subject changes is to emit again; the index either forbids that or forces a retirement that
  describes nothing.
- **`retire` means "the channel has taken this one back"**, which a channel with no display and no
  edit cannot do.

Two facts are fused here, and only one is neutral: "we already delivered subject S", which every
channel needs or a restart reprints the conversation and re-pages the operator at 3am; and "…and it
is still visible at ref R, so revise it there", which is a property of channels that hold an
addressable, editable copy. `chat_delivery` stores the second, and the first only incidentally.

**For an append-only channel the table is not wrong-shaped so much as unnecessary.** What a teletype
needs is a position past which everything has been emitted, which is § 2's per-`(channel,
conversation)` cursor. The correspondence exists _because_ Matrix can revise, and revising requires
addressing. So the table stays and its name is the thing to fix: it is the revision index for
copy-holding channels, not a delivery log. **Do not build the append-only path**: nothing today is
a teletype, and a second mechanism invented now is one to delete later.

**Most of what it stores is write-only, which is the sharper form of the same finding.**
`PendingReply.subject()` mints three kinds — `message:{message_id}`, `turn:{turn_id}` and the single
`status` — so the table takes one permanent row per assistant message per attachment. But the only
reads anywhere are `sync.py`'s two `live(attachment_id, STATUS_SUBJECT)` calls: **every `message:`
and `turn:` row is written and never read**, against <../../../STYLE.md> § Every field needs a
reader. For those rows the table adds exactly one fact over `session_outbox.sent_at`, which the
drain writes in the same transaction: the room's `event_id`. Nothing edits a message, so nothing
wants that id — it is there for the reconciler that replaces the outbox, which does not exist. The
growing set of `(attachment, subject)` rows is a flushed-up-to position materialised as a map, one
row at a time, beside the cursor that holds it properly.

**What genuinely earns a table is the one revisable subject.** `status` is edited in place and
retired; it is the only row whose `sent_ref` is read, and one live row per attachment is bounded. So
the narrowing to consider is not a rename alone: restrict what may be written to subjects the
channel can actually revise, and let the outbox stay the record of what has been sent until the
cursor takes it. Doing that before more callers arrive is cheaper than after — today there are two
readers and one writer.

### A session has no frontend

**"The frontend this session is attached to" is not a thing** (operator, 2026-08-17: _"what is a
session's frontend? why would a session care?"_). It is worth stating flatly because the code reads
as though it were.

What is actually there: `SessionService` holds **one** `ChatFrontend`, the Matrix one, and
`_frontend_for` is not a lookup but a filter — does a channel hold a copy of this session's
conversation? The name promises a mapping where there is a global and a guard.

**A session does not care, and nothing about a session should.** What cares is the turn loop, and
only because the turn loop is doing the channel's job: `report`, `report_silent_turn`, `_speak` and
`TurnStatus` are § 8's "pushed by the turn loop from inside the process that holds the runner",
which is why a fact can reach a room and never a tab. Step 3's D deletes all of it — a subscriber
reads the record and nothing hands a frontend to a turn.

**And the shape is no frontend at all, rather than a better-keyed one.** Selecting by surface says a
channel owns a set of sessions; selecting by attachment says a session has a channel. The invariant
forbids both, however the lookup is keyed. `system_prompt` is the one call that looked honestly
surface-dependent and is not: what the model needs to know is that its conversation is attached to a
chat channel and should be answered in chat's register, which the conversation can say for itself.
The address is not a session's business, which is why `SessionIntroduction.room_id` goes with the
rest.

**Until then, leave it alone.** The right amount of work on `_frontend_for` before step 3 is none:
it is deleted, not improved, and polishing it buys a rename in exchange for entrenching the concept.

## 6. The conversation is identity only

`conversation(conversation_id, operator_id, created_at)` and `chat_attachment` exist (`0064`), with
the partial unique index on `(surface, address) where detached_at is null`. What forces the entity
is a combination rather than a cardinality: **many sessions × many channels**. When the sandbox dies
and session A is replaced by B, what has to move is _the set of attachments_, and a set has no name.
Re-pointing every one of A's live attachments at B works mechanically, but afterwards "this thread"
is recoverable only as a transitive closure over "sessions that ever shared an attachment with…" —
not a join, and no handle to link to.

**Identity and nothing else.** Every fact stays where it already is: what was said on the session,
delivery state on the attachment, rendering on the channel. That is the correct shape for naming a
set whose membership changes over time — the same call this codebase made for `agents` against
`credential_bindings`, where the Agent is identity, the binding holds the state, and rotation
creates a successor binding rather than mutating the Agent (<../README.md> § Canonical Agent
authority).

**What it buys is that the attachment stops moving.** Replacement becomes "a new session with the
same `conversation_id`"; the attachments are not touched, because they were never the session's.

**A session attached to nothing stays expressible** — a conversation with one session and no
attachment rows. That is what an SPA session is, and it costs one row and no decisions.

**Where events live.** On the conversation. "This session's sandbox died", "this replica adopted
it", "the lease lapsed" are _caused_ by a session and are things the operator is told in a room, so
they are conversation facts (<../docs/chat_layers.md>); the session they name is a field, not the
key. Identity-only is a statement about the `conversation` row, not about the record: the log is
keyed to the conversation by step 1 and the conversation table still holds nothing but an id.

`chat_attachment` is authoritative for which room the bot holds, so the rule its `user_id` primary
key enforced — one bot user, one room, ever — is now `bind_room`'s refusal. Losing the schema's
version of it is **the point rather than the cost** (§ 7).

## 7. Settled, and still open

**Settled by the operator, 2026-08-17.**

- **The SPA is a channel like any other, and differs on exactly one axis** (operator: "the spa
  should mostly use the same affordances/operations/protocol as the matrix channel uses"). Same
  `ChatFrontend` port, same subscription, same operations, same neutral vocabulary, same provenance
  pointer on the prompts it sends. The one difference is **whether its position is durable**, and it
  is the reason it gets no `chat_attachment` row: an attachment exists to hold a cursor, a cursor
  exists because a channel holds a copy the console owes work against, and a tab holds no copy.

  Read that as narrow. It is a statement about **delivery state**, not a licence for the SPA to have
  its own protocol — which is what it has today (§ 8) and what this model exists to end. Anything
  the SPA does that Matrix cannot is either this one axis or a bug.

  **The one sanctioned exception is the debug escape hatch**: the SPA may show the underlying
  frames. That is § 11's carve-out — a debug surface may show the raw wire and nothing else may —
  and it survives the "same protocol" rule because it is addressed separately
  (`/api/sessions/{session_id}/frames`, never inside the conversation stream), never load-bearing,
  and labelled as one backend's wire.

- **A conversation never ends.** No `ended_at`, no terminal state — it is an id, sessions come and
  go under it, attachments hold and detach. The consequence still to build on: "start this room
  over" is detaching the address and attaching it to a new conversation rather than ending the old
  one, which the partial unique index already permits.

- **A prompt arriving mid-turn is rejected, not held.** The channel says so and the operator
  re-sends; nothing queues behind a running turn. The notice names the state, so the operator is
  told to wait rather than left guessing.

  **A prompt arriving before a session is ready is held**, which supersedes the same day's ruling
  that it too is refused and the operator re-sends. The queue is the conversation's, so a prompt
  sent while the sandbox provisions waits for a session to claim it (§ 9).

  **Mid-turn steering is the richer answer to what is still refused**, and is not scheduled. Claude
  Code accepts input while a turn is running, so a prompt could join the turn in flight rather than
  be refused. Measured to work (<../../cli_protocol/probes/steering.py>): a prompt written mid-turn
  is absorbed at the next tool boundary and the model acts on it, in one turn with one `result`
  frame — and a turn generating continuous prose has no boundary to absorb at, so it falls back to
  next-turn delivery. That is a runner-protocol capability rather than queueing, and the bridge
  already has an unused input path in that direction. It wants the layering first, because it is
  about what the conversation admits and admission is answered today inside a channel's sync loop.

- **Channel state lives in Postgres, not in the room.** The watermark stays a row; `m.fully_read`
  and per-room `account_data` are not pursued — "postgres is known, state in matrix, who knows",
  reinforced by the mechanics: account data is per-user with no compare-and-set while several
  replicas act as one Matrix user. This does **not** retire reading the room; § 3's correspondence
  reader is how the reconciler learns what the room currently shows, and that is a read, not a
  store.

- **One bot serves many rooms, and that is what parallel sessions are.** `chat_attachment`'s partial
  unique index expresses the rule that is actually wanted: **one live conversation per address**,
  which permits a bot in many rooms at once. What holds the bot to one room today is `bind_room`'s
  refusal and a deterministic ordering behind it, not the schema.

  "Only one session holds a conversation at a time" is unchanged and is a **per-conversation** rule.
  What changes is that the console now runs N of them, one per attached room. Only the operator's
  own MXID still gets Haku into a room, and silently joining a room nothing services stays ruled
  out — it is now prevented by the invite creating the thing that services it, rather than by
  refusing the invite.

  **This promotes on-demand allocation (step 2) from tidy-up to prerequisite.** One room could
  afford a sandbox held open; ten cannot, and a room nobody is talking to must not hold one.

**Still open.**

- **Which notices exist, and what does each summarise?** § 4 proposes one per turn and one per
  session and leaves the set open, along with retire-or-seal for each.
- **Does `sessions.status` survive?** § 10.
- **Which slash-command namespace survives the client?** Element consumes leading-slash verbs it
  recognises and errors on ones it does not — `/me`, `/html`, `/plain`, `/join`, `/invite`, `/op`
  and friends never reach the room — so the choice is a compatibility question rather than a taste
  one. Prefer a prefix Element does not claim (`!haku stop`) over gambling that a verb is free.
- **Can `@haku` set room state at all?** A room-level status (`m.room.topic`,
  `m.room.pinned_events`) is a state event gated by power level. The operator creates the DM and
  holds PL 100 while `@haku` joins at 0, so the console most likely cannot set either today, and
  `channels/matrix/client.py` sends no state events at all. One check in Element answers it; the
  fix is the operator granting Haku PL 50, which is a manual gesture.

## 8. Where this stands

The three layers exist; the boundaries are drawn in the wrong places, and the channel is not one
thing.

### The channel is three mechanisms with three durabilities

| What                                               | Driven by                                                              | Durable?                                                                                                      |
| -------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Assistant replies                                  | `session_outbox` rows, drained by a leader polling at `IDLE_POLL = 1s` | **Yes** — the row is the delivery, redriven after a crash, deduplicated at the homeserver by the row's own id |
| Status line, typing                                | `TurnStatus._run`, a 1s poll **inside the turn loop's process**        | No                                                                                                            |
| Lifecycle notices                                  | `MatrixSessionSupervisor`, woken by `SessionNotifications`             | No                                                                                                            |
| Setup narration, silent-turn, room-binding notices | called from the stack frame that noticed                               | No                                                                                                            |

Everything in the "No" rows lives in `RoomPacer._queue`, an in-process deque given `FLUSH_SECONDS`
on a graceful shutdown and nothing on a SIGKILL. Two consequences, each a thing the reconciler
exists to remove:

- **A dropped notice is silently gone.** `RoomPacer.send` logs and returns once 200 sends are
  queued. A reply survives that (the row is re-claimed); a notice does not.
- **The pacer's budget is an estimate.** `MXSY` (the sync loop), `MXOB` (the outbox drain), `MXNT`
  (the notice reader) and the session lease are independent, so four different replicas can hold
  them and two token buckets can each believe they own the whole room budget — only the homeserver's `retry_after_ms` corrects
  them. A reconciler per `(channel, conversation)` collapses these to one holder, which is the point
  at which a rate budget can be real rather than estimated.

### What is already the right shape

- **Ingress's watermark.** `matrix_sync_watermark` is where the loop reads from, and rejection is
  terminal so it advances every pass.
- **The ordered address.** `session_events.event_seq`, with `PROMPT_ENQUEUED` on it, so the stream
  carries both halves of a conversation.
- **The wake with no payload**, in everything but which layer it names. `session_changed` carries an
  id and nothing else, coalesced at 500ms, and is what replaces the outbox drain's 1s poll; the id
  it carries is a session's, so it moves to the conversation with the log (step 1).
- **A position-addressed read.** `WS /api/conversations/{id}/follow` is the conversation→subscriber
  half working end to end for one consumer kind — a snapshot, then the changes, with a browser on
  the other end of it.
- **The correspondence key.** `EventTag` rides on every event the console sends. Write-only today.

### What is missing

**The outbox drain polls**, at `IDLE_POLL = 1s`, while `RoomNotices` beside it already wakes on
`session_changed`.

## 9. The order

**Do not start with the reconciler.** Each step is independently reviewable, and every one is worth
having even if the loop is never built. The dependency edges are at the end.

1.  **Re-key the neutral log to the conversation, and record what only a stack frame holds.** The
    log is the conversation's record wearing a session's key (§ 1). One migration: `session_id`
    becomes nullable beside a `conversation_id NOT NULL`, the table takes the name of the layer it
    serves, and the authored arm gains the kinds nothing can record today — a turn's lifecycle,
    "a session is being provisioned for this conversation", and the sandbox's setup narration, which
    is a forged `setup_output` frame until it has a row.

    **This is what dissolves three separate dead ends**, which is why it goes ahead of the steps
    that each hit one of them:
    - **A prompt accepted before a session exists** has somewhere to be recorded, so admission stops
      needing a session to name — the question step 2 would otherwise have to settle first.
    - **A provisioning fact becomes recordable**, so an allocator can say what it is doing without a
      channel watching it do it.
    - **Channel notices become projections rather than pushes.** The status line, the setup
      narration and the silent-turn notice each become a fold over the stream, which is what lets
      the turn loop stop holding a channel to push them at (step 3's B and D).

    **`session_messages` moves with it**, for the reason step 2 gives about `session_prompts`:
    `conversation_id NOT NULL`, `session_id` nullable — NULL meaning a user prompt written before
    any session took it. An assistant row stays session-scoped, because its frame pointers are.

    **The wake moves too.** `pg_notify` carries `{kind, session_id}`, so a channel subscribing to a
    conversation subscribes by session; it carries the conversation instead.

    **Done when** a conversation with no session can hold a prompt, a provisioning fact and a line
    of setup narration; when no channel read names a session; and when no authored kind needs a
    session in order to be written. Expand/contract on hot tables, so it stacks rather than races
    (§ 12).

2.  **Allocate a sandbox because there is something to do.** A quiet room holds a sandbox
    permanently: the supervisor provisions whenever the room has no live session, and the warm pool
    is `replicas: 0` so every claim is a cold start. That is ~1 CPU / 2Gi of an 8 CPU / 16Gi quota
    standing idle for a room nobody speaks in. The SPA has a gesture that means "I want a session"
    and Matrix has none, so the supervisor substitutes by assuming demand permanently; the prompt is
    the honest substitute.

    **The demand lives on the conversation, not on a session.** The conversation holds an incoming
    prompt until a session provisions, so a session is created only where there is demand and is
    created straight into `provisioning` — an unclaimed prompt against a conversation with no live
    session is the signal, and it is the same on every surface. It is swept by a channel-neutral
    `SandboxAllocator` (`x/sandbox_allocation.py`) under its own `SBOX` election — never from the
    request path, and never from a channel's supervisor, which now only announces what it sees.
    `POST /api/conversations` therefore returns a conversation holding no sandbox, exactly as a quiet
    room does. The rejected alternative was a session created in an `idle` status that a prompt then
    bought a sandbox for (#4231, closed): it made "exists" and "is wanted" two different facts about
    one row, and the enum member it needed is gone again. **Cost, stated plainly:** the first message
    after quiet pays the full cold start. Measure it rather than assume it away. **Done when** a
    quiet room holds no sandbox, the first message provisions one, and a prompt sent while it
    provisions is answered rather than refused.

    **`session_prompts` cannot move alone.** It holds no text by design: `message_id` FKs
    `session_messages` so the queue and the transcript cannot come to disagree — which is why
    `session_messages` is re-keyed by step 1 and this step's queue is conversation-keyed from the
    start.

    **Where the first prompt is recorded is settled by step 1.** `prompt_enqueued` is written in
    `enqueue_prompt`'s own transaction, so the ordered stream gains the operator's turn exactly when
    the transcript does; with the log keyed by conversation, a thread's first accepted prompt has a
    row to be written into before any session exists. That is what makes this step's queue
    expressible at all.

    **The SPA posts to a session-addressed route** (`POST /api/sessions/{session_id}/messages`), so
    a conversation-addressed route has to ship and be adopted across a `maxUnavailable: 0` roll.

    **The sweep can be lifted rather than written.** `x/sandbox_allocation.py` and its test on
    `claude/haku-idle-sessions` already carry the election, a wake on `SessionEventKind.PROMPT`
    with a periodic backstop, and failure backoff. Only the query changes.

    **What it retires.** `IngressLedger.unanswered`, `MatrixTurns.re_offer` and
    `_re_offer_unanswered` go outright — a prompt that outlives its session was never in the wrong
    place — and the duplicate transcript row a re-offer mints goes with them.
    `uq_session_prompts_unclaimed` comes to express one prompt in flight per _thread_. And
    `PromptRejection.NO_SESSION` and `SESSION_NOT_READY` lose their reason.

3.  **Matrix becomes a subscriber** (§ 2's primitive) for every kind, not just the one
    `RoomNotices` reads. One loop per `(channel, conversation)`, reading the record from its cursor
    instead of being handed events by the turn loop, woken by `session_changed` and by inbound
    events with the 1s poll demoted to a fallback. Here the four elections collapse to one, the
    three egress mechanisms become one difference calculation, the pacer's bucket stops being an
    estimate, and the browser stops being the only consumer that reads the record.

    Four parts, split by which forbidden edge each one removes:
    - **A — the loop.** One subscriber per `(channel, conversation)` over the kinds the record
      already carries, and one election in place of four. It removes no edge by itself; it is what
      the other three are built on.
    - **B — the turn's live state.** The status line and the typing indicator become a fold over the
      stream instead of `TurnStatus._run` polling inside the turn loop's process. **Blocked on step
      1**: a turn's live state is the open `session_turns` row and nothing else, so a room can reach
      it only by reading a session-keyed table. There is nothing to fold until a turn's lifecycle is
      on the stream.
    - **C — replies stop being pushed.** `_enqueue_reply` stops reading the attachment's address;
      the channel derives what the room is owed from the record and its own cursor.
    - **D — the turn loop stops holding a channel.** `_frontend_for`, `ChatFrontend`, `report`,
      `report_silent_turn` and `SessionIntroduction.room_id` go. **Blocked on step 1** for the same
      reason in the other direction: setup narration exists only as a forged `setup_output` frame
      and a silent turn's notice only in the stack frame that noticed it, so deleting the push
      deletes the fact. Recording them first is what turns a deletion into a projection.

    **`_frontend_for` is deleted in D, not improved** (§ 5): the question "which frontend is this
    session's" stops being asked rather than being answered better. Three things go with it, none
    obvious from the symbol: a **green test** pinning the surface-matching contract, which deleting
    the code deletes; **two <../x/README.md> sentences** restating the doctrine; and
    `SessionIntroduction.room_id`, whose only builder is the Matrix channel and which therefore
    stops being the thing a system prompt is selected by.

    One correction to make here rather than carry: <../x/README.md> claims "the live path no longer
    knows that `assistant`, `stream_event` and `result` exist", while `_run_turn` reads `subtype`,
    `stop_reason` and `result` straight off the payload. The code is honest — `_CompletedTurn.frame`
    calls itself the escape hatch — so the README is the false half, and a second backend would
    degrade silently in both directions until it is fixed.

4.  **Notices as spans** (§ 4), once 3 exists: one work notice per turn, one lifecycle notice per
    session, **each body a fold over the subscription stream**, each retired or sealed. This is
    Matrix's streaming — the granularity a channel that holds a permanent, federated copy can
    afford. The fold is not optional; editing already-posted notices is, and a v0 that appends one
    notice per noticeable event is the accepted lesser form. Split the fold from the send path in
    the same PR, because the fold is where this step's tests live.

5.  **Many rooms at once** (§ 7's ruling). `bound_room` stops answering with the one room and
    `bind_room` stops refusing the second, so an invite mints a conversation; the supervisor fans
    out over live attachments instead of supervising one binding; `MatrixSurface` takes its address
    rather than claiming it has one by construction. The `MXSE` advisory lock stays
    global — one election supervising every attachment is cheaper than a lock per conversation, and
    the thing that must not be global is the _lease_, which is already per session.

    **Depends on 2 and on 3**, both of which the dependency line is easy to get wrong. Ten rooms
    each holding a sandbox is the failure this would ship without 2. And an audit found **eleven**
    things that assume one bot serves one room, seven of which are per-bot process state that step 3
    deletes: `_status_body`, `_last_announced`, the single `RoomPacer`
    with its one collapsing status slot, `_serviced`/`_live_room`, and `RoomOutboxDrain` claiming
    only `bound_room()`'s rows. Every one fails **silently** with a second room — ingress dropped to
    a `logger.warning`, a status line that never appears in room B, an edit addressed at another
    room's event id, replies that sit unsent forever.

6.  **The console relays an operator's message into the room.** The console holds one Matrix
    credential, `@haku`'s, and cannot post as the operator's MXID — so a console-originated message
    either does not appear in the room at all, or appears under Haku's account. Not posting it is
    ruled out, because the operator's own Element would then show half a conversation. So `@haku`
    posts it under a `relay` kind, tagged like every other console-authored event and rendered so
    the room states its true provenance.

    **Under step 3 the send posts nothing.** It enqueues the prompt, in one transaction, and stops;
    the room is then behind the transcript by one message, which is a divergence the reconciler
    already exists to close. That is what the model buys here: no "enqueue then post" order to
    choose, no partial failure where one landed and the other did not, and no bespoke retry.

    Three things to get right. Ingress needs no change — Haku's own sender is already excluded, so a
    relay cannot loop back and be answered twice. Provenance is the transcript message id and "it
    came from the console", never a fabricated event id: the relay posts strictly after the prompt
    is enqueued, and the turn must never wait on the room to obtain one. And a refusal must reach
    the operator rather than be swallowed — Matrix ingress absorbs one by not advancing the
    watermark, and a console send has no homeserver behind it. Plain text for now; the operator
    writes into a textarea, so a plain body is honest.

    **Rejected, and priced already:** giving the console the operator's Matrix credential, and an
    appservice with a puppet MXID. The first breaks the single-holder property for a send button;
    the second reverses the whole `/sync`-over-appservice decision for one.

7.  **Slash commands**, which are how Matrix gets the actions the console has. The parity gap —
    abort, new session, close — is one missing affordance, not three: a way for a room message to
    mean something other than "talk to Haku". They are **ingress interception, not an agent tool**:
    a command is recognised and consumed by the harness before batching, so it never reaches the
    agent as a prompt, and the agent's read-only tool surface is untouched. Authorisation needs
    nothing new — it is a DM, the sender maps to an operator identity, and an unmapped sender gains
    no authority. What must not follow is approvals: a slash command is an operator gesture against
    the _session_, and a Matrix message is never consent for a tool call. Namespace choice is § 7's
    open question. Abort is the one worth having first. Depends on nothing here.

8.  **The session link in the room's startup notice**, and interlinking generally: room notice →
    console session, console session → the room (a `matrix.to` permalink the client already builds),
    session → its tool calls, tool call → the session that made it. The last has its precedent —
    `/_console/tool-calls/<tc_…>` opens the drawer on that exact call. A Matrix event is permanent
    and federated, so post links under routes chosen to survive, or not at all.

9.  **The frame log stores one thing, and the runner numbers it.** Two changes, deliberately one
    step, because they share a cause — the recorder sits _above_ the bridge envelope and
    structurally cannot see one — and therefore share a fix: moving that sink down onto the socket.
    § 13 holds the design and the release schedule.

10. **`sessions.status` becomes derived timestamps** (§ 10).

11. **Finish the legacy purge.** `ck_session_frames_wire_numbered` is declared nowhere — its rows
    are gone, so all that stands between it and the database is writing it in the ORM as well as
    the migration, once
    `SELECT count(*) FROM session_frames WHERE runner_seq IS NULL AND direction = 'from_agent'`
    returns zero. `test_message_tool_calls_migration.py`, `test_neutral_turn_usage_migration.py`,
    `test_session_claim_cleaned_at_migration.py` and `test_frame_runner_seq_migration.py` each
    assert a backfill or a nullability the schema has since moved past, and go outright.

12. **The read surfaces stop maintaining two model families over one query** (§ 14). Two small
    changes, neither of which needs a transport decision.

**Smaller, and each landing with the change that creates it rather than as a standalone reshuffle:**

- **Remove the `asyncio.wait` abort dance in `_run_turn`.** Unmounting the SSE route made it
  removable and nothing has removed it. It collapses properly at step 3, where an abort becomes an
  intent the transport writes and the CLI's answer comes back as frames.
- **Split `session_runtime.py` further.** `handle_runner`'s admission and finalisation are a
  connection's lifecycle and `_run_turn` is a frame reducer; those are two files' worth of concern
  in one class.
- **"Surface" names five things and "turn" three.**
- **Thread one projection state across a turn.** The cursor rests on per-frame seeding, so every
  frame boundary is a finish boundary; threading makes re-projection from
  `session_turns.first_frame_seq` the answer instead, measured at ≈1 µs/frame and affordable. Two
  loop-side bugs block it and `session_runtime._projected` names both. Pull it in when a message
  spanning frames needs to be one row.

**Dependencies.** 1 gates 2, gates 3's B and D, and gates 5. 2 gates 5. 3 gates 4 and 5. Everything
else — 6 through 12 and the smaller items — depends on nothing here and can be dispatched in any
order. 3's A and C are not gated on 1 and can be built beside it.

## 10. `sessions.status` is derived, and lossy

**Derived.** `responding` already is: no path writes it to the column, and `session_view` computes
it from an open `session_turns` row — the SPA switches on the API field, so the column stopped
carrying it with no frontend release. Of the rest, `provisioning`/`ready` follow from
`bridge_connected_at`, and `failed` from `error IS NOT NULL`. Only `closing` and `closed` have no
evidence in the row today.

**Lossy, which is the stronger argument.** `provisioning` stands for several distinct facts — claim
submitted, pod scheduled, sandbox running, runner dialled back — and the row records exactly one of
them. So a session that never came up reports `failed` plus free text, where the operator wants
"the claim was never satisfied" told apart from "the sandbox ran and the runner never dialled".

**The shape that replaces it** is the timestamps that actually happened — `claim_submitted_at`,
`sandbox_ready_at`, `bridge_connected_at`, `close_requested_at`, `ended_at` — with the enum computed
in one place and kept as the wire vocabulary. Two things fall out: the invalid states the current
shape permits (`closed` beside an open turn, `ready` beside a non-null `error`) stop being
representable, and `idx_sessions_expired_lease`'s partial predicate stops listing statuses, so
adding a member no longer edits an index.

**Split it.** Adding the terminal timestamps and deriving `closing`/`closed`/`failed` is one change;
dropping the column is the follow-up once every member computes. Both are expand/contract on the
hottest table.

## 11. What it looks like when it is done

### The shape

A **conversation** is an id. Under it, sessions run one at a time and end; attachments hold at the
same time and detach. Both point at the conversation; neither points at the other.

```text
conversation ──< session   (serial: one runner each, replaced when the sandbox goes)
             └─< attachment (concurrent: a room, a browser, whatever comes next)
```

Every fact is written once, into the conversation's record — folded out of a session's frames, or
authored where the console was the only witness. Every channel reads that record through one
subscription (§ 2) and renders it in its own vocabulary. Nothing is pushed at a channel; a channel
is told to look.

### The invariants to hold it to

These are the acceptance criteria, and each names something that is false today:

- **No code outside a channel names a channel's address.** `_enqueue_reply` stops reading the
  attachment's address; the turn writes the record and nothing else.
- **No code inside a channel names a session.** No session-keyed read, no session id in a durable
  channel artifact, and no channel creating or tending a session.
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

The last two are what "N backends × M channels, additively" actually means, and they are the reason
for the whole exercise.

### The one thing a channel may know about a backend

**A debug surface may show the raw wire, and nothing else may.** The frame inspector is this:
`/api/sessions/{session_id}/frames` serves `SessionFrameView.payload` as `dict[str, Any]` — the
frame whole, deliberately unclipped, because `session_messages` is a lossy projection of it and
clipping there would reintroduce the loss one level down.

Three conditions keep that from becoming a hole, and the inspector meets all three today:

- **It is addressed separately.** A provider payload never rides inside the conversation stream, so
  a channel cannot consume one by accident — it has to ask a different route for it.
- **It is never load-bearing.** No rendering decision, no notice body, no delivery decision reads
  it. A channel that cannot reach the inspector loses nothing but a debugging affordance.
- **It is labelled as one backend's wire**, not as the conversation.

**The vocabulary has been audited against this rule and the rule the audit produced is the part
worth keeping**, because it is what decided the cases that looked alike: **the line is not how
Claude-shaped a thing is, but where the shape lives in the type.** `ToolCallCompleted.structured` is
_also_ one tool's shape and it stays, because it sits behind `Json` — a per-tool payload is
sanctioned (a channel rendering a Bash result's `stdout` knows Bash, not Claude) while a per-tool
shape promoted to a typed member is not. That bounds the leak surface exactly: `Json` marks three
fields, and `Projection.unprojected`'s keys are a fourth the type does not mark. `unprojected` is
produced by the adapter — the one component allowed to be provider-shaped, since translating is its
job — so it reaches no channel and breaks no invariant.

**The trap the audit left behind, worth carrying because it will recur:** a name-based search does
not establish that a field has no reader when a projection renames it at a layer boundary.
`unprojected` is renamed downstream three times — `transcript_entries.unreadable`,
`TranscriptSlice.unreadable`, `TranscriptPage.unreadable` — so grepping the name finds only tests,
and deleting it would have removed a live wire field an agent already receives.

**What enforces the rule does not exist yet.** `x/frame_projection.py` imports
`x/claude_code/projection` directly, so there is no seam at which a second backend's adapter could
be selected — the fold is spelled in terms of one provider by construction
(<../../runtime/x/bridge/docs/second_backend.md>). Until `CliBackend` grows that member, the
invariant is a convention held by review rather than by the type system, which is the same shape of
gap that let the turn loop become a frame interpreter in the first place.

### What disappears

**Tables and columns**

| Gone                                     | Replaced by                                                                                                              |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `matrix_conversation` (all four columns) | `chat_attachment`, whose partial unique index is what "one session holds it" means                                       |
| `sessions.surface`                       | the attachment                                                                                                           |
| `session_outbox`                         | the reconciler derives what it owes by comparing the record to the room; a queue that holds no facts need not be durable |
| `sessions.status`                        | the timestamps of § 10, with the enum computed                                                                           |

Not gone, and deliberately: `matrix_sync_watermark`, `session_events`, `session_frames`, the lease.

**Mechanisms**

- `RoomOutboxDrain` (`MXOB`) and `RoomNotices` (`MXNT`) — one election instead of four.
- `TurnStatus._run`'s in-process poll, and the turn loop calling `show_status`/`set_typing` at all.
- Every per-process latch that stands in for durable state: `_status_body`, `_last_announced`.
- `RoomPacer` as a queue of opaque callables — a budget the reconciler spends, not a deque of
  closures it cannot inspect or squash.

### How to tell it is finished

Six behaviours. The first four are the surfaces working; the last two are what proves the
architecture rather than the features, and each is the acceptance test for the thing that forced it.

1. **The Matrix surface still works.** `x/channels/matrix/test_fullstack_e2e.py` against real
   Synapse stays green throughout. A regression gate, not a new test.
2. **The web surface works**: one merged surface lists conversations, creates one, sends, aborts,
   and shows a transcript.
3. **The web surface streams per token.** A message being written grows in place at
   `COALESCE_WINDOW` granularity today, which is the weaker form; the delta kind § 2 designs is what
   tightens the assertion.
4. **Both surfaces show the operator's own prompts, wherever they were sent from** (§ 4). A prompt
   typed in the SPA appears in the room; one sent from the room appears in the tab; neither appears
   twice on the surface it was typed on. The first case does not work at all today, and the third is
   what the provenance pointer exists to make true.
5. **One conversation, two surfaces.** A room and a browser both open, either can prompt, both show
   the same account. This is what the conversation entity is for, so it is its test.
6. **A session replacement is invisible to both.** The sandbox is killed mid-turn, a new session
   takes over, and both surfaces show the restart without either being told twice.

**Write these per step, not at the end.** A failing test cannot land on `devel`, so each step's PR
carries the test that proves its own part, and 4, 5 and 6 land as end-to-end tests with step 3 —
where a channel starts reading the record rather than being handed the half the turn loop
produced.

**Encode the invariants as tests too**, because each is false today and would otherwise creep back
silently:

- `session_store` contains no reference to `room_id`, and nothing under `x/channels/` names
  `session_id` — two structural tests, and between them the whole of the two forbidden edges.
- **Notice bodies are folds**, tested as folds: a list of `ConversationEvent`s in, a body out, no
  room and no database. The cases worth writing are the ones an end-to-end test cannot provoke on
  demand — a forty-call run collapsing to a tally, a session replaced mid-turn, a turn aborting
  between two tool results.

## 12. How this executes

§ 9 is the dependency order. This is how it is worked: what fans out, what cannot, and where the
position is kept so a session that dies mid-flight loses nothing but its own context.

### The bottleneck is migrations, and it is narrower than the step list suggests

**Migration-bearing PRs are stacked rather than raced**, and what a branch owes the chain when it
picks a revision id and a parent is <../AGENTS.md> § Adding a migration while other branches hold
one.

**A permissive schema change is safe alone.** A widened CHECK forbids nothing the previous image
writes and has no reader, so it can merge on its own and the writers that use it become independent
pure-code PRs that can be written at once. That is what turns a serial stretch into one migration
and a wide fan.

### What every agent is told

- **Never merge.** The operator merges. Say plainly when a merge has an ordering constraint.
- **Amend this plan in your own PR** when implementation shows it is wrong, and say so in the body.
- **Abort and report** rather than deciding anything architecture-level — what a session or
  conversation _is_, a new table, or a decision that binds other steps.
- **Collisions are expected**, and coordination is not the fix. Whoever lands second rebases, and a
  conflict between two deletions resolves to _both_ deletions — taking one side wholesale silently
  reverts the other and CI goes green anyway, which is the failure mode to watch for.

### Where the position lives

This document. A session running this loses its context; the plan, the rulings and the order are
what survive. **Each PR that completes a step deletes that step here**, so what remains is the work
that remains.

### Checking the world instead of remembering it

**Deploy gates progress on their own. Re-derive them; never carry them.** Several steps are gated on
"the release that stopped writing X has converged", and that becomes true without anyone doing
anything — so a gate checked an hour ago is not a fact, it is a stale reading. The check is two
commands and there is no excuse for inferring it:

```bash
kubectl get pods -n haku-console -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}'
git merge-base --is-ancestor <merge-commit> <deployed-commit> && echo converged
```

Read the commit suffix off the image tag and resolve it by **ancestry**, not by comparing tag
timestamps or trusting a PR title. Both replicas must report one tag: mid-roll, the older pod is
still serving.

**Production is queryable, so check the data before scheduling work against it.** The console's MCP
server answers over the network with the bearer in the `haku-console-agent-api` secret in the
agent's own namespace, against `https://haku.allegedly.works/mcp`. Its `haku_conversations__*` tools
read production sessions, transcripts and frames, which is how a question like "are there rows of
this kind left to delete" gets an answer rather than an estimate. `hostexec__bash` is approval-gated
and pages the operator, so it is for when a shell is genuinely required and not before.

**Fan out hard, then babysit what you dispatched.** Agents work in their own worktrees and PRs go
out in parallel; the operator merges. The half that is easy to drop is the second one — a PR is not
delivered when it is opened, it is delivered when it is green, rebased and still mergeable, and
`devel` moves under all of them. That means a standing sweep: merge-cleanliness with
`git merge-tree --write-tree` and its **exit code**, every PR's checks with `perPage: 100` because
`Pre-commit checks` lands on a later page, and the migration branches simulated **against each
other** rather than only against `devel`. Prune finished worktrees as you go.

## 13. The frame log: one vocabulary, and the runner's numbering

Step 9's design. Two changes that share a cause and therefore a fix.

### `kind` holds two vocabularies

`session_frames.kind` holds **two different discriminator vocabularies**, because two unrelated
sinks write to it:

| Writer                                                  | Sees                                      | Writes into `kind`                             |
| ------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------- |
| `RolloutRecorder._record`, a `FrameSink` on `ClaudeCli` | CLI protocol frames only, by construction | `payload["type"]` — the CLI's vocabulary       |
| `_progress_reporter.report`, by hand                    | one decoded line of a `SetupOutput`       | `"setup_output"` — the bridge's `kind` literal |

**The intended shape: the table is the log of the bridge.** `kind` becomes the envelope
discriminator (`claude`, `setup_output`, `hello`, `start`, `end_input` — `protocol.py` owns the
list) and the CLI's own `type` gets a column of its own. Two columns, each answering one question,
and any future runner-originated frame has a home instead of a special case.

**What this costs, stated before starting.** The recorder is a `FrameSink` on `ClaudeCli`, which
sits _above_ the envelope and structurally cannot see one — so the sink moves down to
`WebSocketTransport`. The dedup answer moves with it: `received()` returns False for a replay and
`ClaudeCli._read` uses that to skip routing, so the transport takes over both. It also means
recording envelope kinds nothing reads today (`hello`, `start`, `end_input`); that is the point of
the design rather than a cost, but it is new rows.

### The number is the runner's, not Postgres's

**Decided** (operator, 2026-08-16): _"I do think we really should do the runner owned numbering and
use it for existing offsets, it also makes catch up trivial."_ So `frame_seq` is minted on the wire
and becomes the log's own offset, rather than a second column beside a database-assigned one. Today
it is `BigInteger, Identity(always=True), primary_key=True`, travelling back to the client on
`RecordedFrame.frame_seq` after the `INSERT`.

**The motivating property is catch-up, and nothing else gives it.** A dense counter the runner owns
turns reconnect into _"send me everything after N"_: the runner answers from its own retained window
with no database round trip and no reconciliation. An `Identity` cannot be asked that. It is sparse,
so a hole in it is not evidence of anything; it is unknowable until after the write; and it is the
console's fact about a row rather than the wire's fact about a frame. Today's adoption therefore
re-sends the **whole** window every time and leans on `frame_uid` to sort it out — which works for
the classes that carry an agent-assigned id and cannot work for the ones that do not.

Three secondary properties come with it. The number exists the moment the frame is read off the
socket. It is true arrival order rather than insert order. And `ReceivedFrame.frame_seq: int | None`
becomes structurally impossible rather than avoided by convention.

**Where the counter lives.** `WebSocketTransport` is the _console_ side of the bridge socket, and
the console is the end that is replaced mid-conversation — that is what a roll is — while the runner
process outlives every socket it serves, by construction. A cursor a reconnecting console hands back
has to be a number its _peer_ minted, or the peer cannot act on it. So **`runner.py` mints, at the
point a frame is put on the wire, and stamps it on the envelope**; `WebSocketTransport` is where the
number is read and recorded. Numbering at send rather than at read also fixes it once: a frame
retained in the replay window keeps the number it went out under, so the console that adopts the
session and the console that died see the same integer for the same frame.

**Seeding across reconnect.** A runner's counter is per process, which is per sandbox, which is per
session — so in the happy case it needs no seeding. What needs seeding is the case the design exists
for: the console must be able to say where it has got to, and two consoles may be replaying one
runner's window at once during a roll. So the cursor is **per session, not per connection**.
`ClaudeLaunch` (`start`) carries it as `resume_from`, computed by the console as the highest
runner-minted sequence recorded for that session; `start` is sent on every connection, so this needs
no new frame and no new round trip. The runner takes `next = max(next, resume_from + 1)` and replays
only its retained frames above `resume_from`. Two consoles racing compute the same cursor from the
same rows, and a runner whose process really did restart is seeded back above what the console holds
rather than colliding with it from 1.

**Why this rides as an added field and not a `PROTOCOL_VERSION` bump.** `SUPPORTED_VERSIONS` is a
single element, so a bump does not negotiate — it _refuses_ the peer on the other number. A runner's
image is fixed when its SandboxClaim is created and a live session outlasts console releases for as
long as a replica tends it, so a bump kills every session in flight on the release that ships it.
`protocol.py` already made the opposite trade for exactly this reason: `extra="ignore"`, so an
unknown _field_ is dropped by a peer that predates it while an unknown _kind_ still fails closed.
`seq` and `resume_from` are optional fields on frames that already exist, and both directions of
skew degrade to today's behaviour.

### What "dense" buys, and what enforces it

Dense means consecutive frames from one runner differ by exactly one, so a hole is evidence rather
than noise. Two checks fall out, and they are different questions:

- **Live contiguity.** Within one connection, the transport compares each frame's `seq` against the
  last. A hole means the socket delivered out of order or the runner's buffer overflowed — neither
  should be possible, so it is a bug report, not a recoverable state.
- **Resume completeness.** On adoption, the runner replays from `resume_from`. If the oldest frame
  it can still offer is above `resume_from + 1`, its window has rolled past what the console
  recorded and those frames are gone for good. That is the case today's design cannot even see.

**What a consumer does with a gap.** Not "carry on quietly", which is what happens now. The
projection over a gapped log is not trustworthy — a message can be missing the frame that closed it
— so the gap is recorded as a session event and surfaced in the frame inspector, which is the
surface an operator already opens to appeal a transcript. Escalating further (failing the turn) is
deliberately not proposed: a lost `stream_event` is cosmetic while a lost `result` is not, and the
log cannot tell which is missing.

**Neither check can be written before the cutover.** Both read a run of numbers and ask whether it
is dense, and neither run is dense yet. The console's recorded numbers are not: a `SetupOutput` is
numbered by the runner like everything else, but it reaches the log through the progress reporter —
where one frame decodes into however many complete lines it finished — so its number is on no row,
and every bootstrap leaves a hole that means nothing. The wire's numbers are not either on the
connection that matters: the replay window retains only `replayable` frames, so an adopted
connection is handed a sequence with a hole wherever a delta or a narration line was. Adding a check
before the cutover reports on narration and on the replay window's own design, which is a warning
nobody can act on — the fastest way to teach an operator to ignore it.

**One thing dense numbering buys that `frame_uid` never could.** `frame_identity.py` argues, and it
is right, that a `stream_event` must not be replayed: it has no agent-assigned id, and
`streamed += delta` double-appends. A dense sequence is an identity for the frames that have none —
not of their _content_, which is what the module correctly refuses to invent, but of their
_position_. Once the console dedupes on `(session_id, frame_seq)` rather than on `frame_uid`, a
replayed delta is refused by the key before it can reach the loop, and the "never replay a delta"
rule is a bound on the window's size instead of a correctness argument. That is also the point at
which `REPLAY_WINDOW = 500` has to be re-sized or given a byte budget, because a delta-heavy turn
runs to thousands of frames.

### The primary key

`frame_seq` is the primary key, and it is read by `FrameCursor`, both keyset reads, `session_turns`,
`session_messages.source_*`, the `haku_conversations` MCP tools, and the frames page.
Client-supplied values mean dropping `Identity` and enforcing uniqueness per session instead of
globally.

**The constraint that shapes it: there are two minters into one space, and there is no way around
that.** The runner numbers what crosses the socket, but the console writes rows the runner never
sees — a console→CLI write is recorded before the runner has it, and a console-origin session event
crosses no wire at all. Two independent minters cannot share one dense integer space: either they
partition it or the key carries a tiebreak. Partitioning by a reserved stride was considered and
rejected — the band between two runner frames is bounded by hope, and an overflow is a primary-key
collision on the hot path. So:

- **`frame_ord SMALLINT NOT NULL DEFAULT 0`**, and the primary key becomes
  `(session_id, frame_seq, frame_ord)`. A runner frame is `frame_ord = 0`. A console-origin row
  recorded while the console's high-water mark is _N_ takes `(N, k)` for the next free _k_, so it
  sorts strictly after runner frame _N_ and strictly before _N+1_ — the same fidelity insert order
  gives today, stated rather than implied.
- **Every cursor still names one integer.** `FrameCursor`, `before_seq`, the MCP tool arguments and
  the frontend are unchanged: a cursor of _N_ is `(N, 0)` in the composite. Only the `ORDER BY` and
  the key change.
- **`session_messages.source_*` stays two integers**, and an inclusive range over a composite order
  is still well defined — the range is over positions, and a position is a `frame_seq`.

**No session may hold both numbering schemes, and the fault is loss rather than untidiness.**
Identity values are global and run far above 1; runner values are per session and start at 1.
Uniqueness and ordering are per session, so the two never have to be comparable — but a session
carrying both breaks catch-up outright: `resume_from` is that session's `max(frame_seq)`, and a
cursor in the tens of thousands selects nothing from a runner window numbered from 1, so the
reconnect replays nothing and whatever the dead replica missed is gone.

**Dropping `Identity` is the one step that is not additive**, and the trick that makes it roll-safe
is that it does not have to be a drop. `ALTER COLUMN frame_seq DROP IDENTITY` followed by
`SET DEFAULT nextval(...)` on a sequence seeded above the current maximum leaves an old replica —
which inserts without naming the column — behaving exactly as before, while a new replica may supply
its own value. `GENERATED ALWAYS` is what refuses a supplied value; a plain default does not.

### The release schedule

`maxUnavailable: 0` means old replicas run against the new schema for the length of every roll, so
R3 is gated on R2's roll having **converged** — every pod on an image at or after it — rather than on
a release having elapsed. A stalled roll leaves the old replica serving, which is exactly when "one
release later" is the wrong gate.

| Release               | What lands                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Additive?                                                                                                                                                                                 |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **R1**                | The wire and the runner: optional `seq` on the runner→console envelopes, optional `resume_from` on `start`, the runner minting and replaying from the cursor. Separately: `cli_type` added, dual-written, every reader onto `coalesce(cli_type, kind)`, backfilled where `kind <> 'setup_output'`; and the console computing `resume_from` from `runner_seq`.                                                                                                                           | **Yes.** New optional fields, new nullable columns. Old replicas and old runner images unaffected.                                                                                        |
| **R2 — the cutover**  | The sink moves to `WebSocketTransport`; `kind` becomes the envelope discriminator, typed `BridgeFrameKind`, with the coalesce dropped and the CLI's own `type` left in `cli_type`. `frame_ord` and the composite primary key; `Identity` demoted to a plain sequence default. Frames take `frame_seq` from the envelope, dedup keys on position rather than `frame_uid`, both density checks report, `REPLAY_WINDOW` is re-sized, and `runner_seq` stops being written and is unmapped. | Schema yes, behaviour no — the `kind` flip is safe **only** because R1 stopped every reader depending on it. An old replica still filtering `kind = 'assistant'` would mis-decide a turn. |
| **R3 — the contract** | Everything an old replica was still touching through R2's roll: `frame_seq`'s sequence default and the sequence behind it dropped, so an insert that names no number fails instead of inventing one; `runner_seq` and its index dropped.                                                                                                                                                                                                                                                | Removals, so gated on R2 converging.                                                                                                                                                      |

**Two releases, and it is replicas that force the second rather than rows.** An old replica inserts
without naming `frame_seq`, and still writes `runner_seq` — so R2 cannot drop what it would need.
That is why `Identity` is demoted to a plain default rather than removed, and why a column
SQLAlchemy still names in every `SELECT` is unmapped in R2 and dropped in R3.

**Purge on both sides of R2's roll.** `DELETE FROM sessions` is the whole operation: `ON DELETE
CASCADE` takes `session_{frames,messages,turns,prompts,events,outbox}`, and
`matrix_conversation.session_id` is set NULL, after which the supervisor provisions a replacement
session against the same room. Run it before, so no live session carries identity-numbered frames
into the roll; and again once R2 has converged, because the roll is itself a window in which an old
replica can create a session and record identity frames into it. The window is the roll's length and
what it costs is one session's replay. The index's `chat_*` tables hold `session_id` as a bare UUID
with no foreign key, and repair themselves within one sweep — `sync_chat` computes
`forgotten = indexed sessions − sessions the source still shows` and calls `forget_chat_sessions`
before indexing anything.

### The invariant the read path owes

**Nothing in the read path may assume `frame_seq` is 1-based, comparable across sessions, or by
itself a row identity.** After R2 every session's numbers do start at 1, which is exactly the moment
someone writes a reader that relies on it — and `frame_ord` means one `frame_seq` can name more than
one row of a session. A test over a session with deliberately sparse, high-valued frames and a
console-origin row at `(N, 1)` states that on purpose rather than leaving it true by accident of how
the readers were written. The fixture stays sparse even though production holds no sparse session: it
is a statement about what the readers must tolerate, not a sample of the table.

## 14. The read surfaces, and the line between them

The console reads its session corpus through two surfaces that answer nearly the same questions off
the same tables — REST for the browser, `haku_conversations` over MCP for agents. **The duplication
is two Pydantic families over one query, not two implementations**, and the recommendation is to
make the _store_ singular rather than the API.

The boundary, stated as a rule:

- **REST owns every read whose answer depends on _who is asking_** — the inventory, the interpreted
  transcript, and the frame inspector. An in-process MCP server is handed a **credential, never a
  caller**, so operator scoping is not expressible there today.
- **MCP owns every read whose answer is the same for everybody with the authority to ask it** —
  reflection and semantic recall (`haku_index.search`), which has no REST twin and should never grow
  one.
- **`/api/events/ws` says _what changed_; the read surface says _what it is_.** A
  `SessionChangedEvent {session_id}` is an invalidation, not a payload.
- **Mutations, the WebSocket, and anything the service worker touches stay REST**, always.

Two things are left, neither of which needs a transport decision:

- **Make the reader reflectable.** `build_schema_servers()` leaves `conversations` and `index`
  unset, so the conversation tools are absent from both generated catalogs. Passing the same inert
  object as `conversations=` (and `index=`) registers them for reflection: one file, zero production
  behaviour. Every JSON Schema keyword those models produce is already in the reviewed
  `_FRONTEND_SCHEMA_KEYWORDS` allowlist, so this should generate without adapter work — and if it
  does not, that is a cheap and decisive answer.
- **Delete `ConversationTurnView`** and return the frame range that `TurnRecord` already carries, so
  the detail view's turns link to the frame inspector. One store method, one model, both surfaces.
  This is the actual duplication.

**Not planned: moving `/api/conversations*` onto MCP.** Revisit only when **both** hold: an in-process
server can be handed the acting principal (a fourth `InProcessCredentialKind`), and the tier decision
function from <../../plans/information_trust_tiers.md> exists at the one console call site it is
meant to live at. Until then, moving an operator-scoped browser read onto a deliberately unscoped
tool either widens what the console shows or forces scoping into the surface designed not to have
it. Three costs a plan should state rather than discover: the browser's argument shape would be
downstream of an agent policy file (`_is_passthrough` reads the auto-approval registry, so dropping
a tool from Haku's policy silently makes the console page need the `{input, rationale}` envelope);
the MCP surface's prose is written for an LLM reader and `MAX_PAGE_BYTES` exists to protect a model's
context; and a page moving to MCP trades a typed 404/409 for a joined-text error blob and loses the
generated `paths` typing.

## 15. The standing rules

Not scheduled items — constraints that govern everything above.

**Every outbound channel write is recorded first and sent from the record** (operator, 2026-08-16:
_no events should be written directly into Matrix without going through our database, because
Matrix is just one of pluggable backends — channels_). A write that goes straight to the homeserver
is invisible to every other channel, unrecoverable across a crash, and unprojectable. The test of
compliance is not "does it work" but "could Telegram show it" — which makes the easy-to-forget
writes the interesting ones: typing indicators, edits, redactions, invites, and the console's own
notices. <../debug/channel_write_audit.md> is the inventory.

**The projector is single-writer per session.** The lease gives that, and it is the reason none of
this needs the fold to be re-runnable. An expired lease means unowned rather than dead, but the
property still holds: `authenticate_bridge` admits one holder at a time while a lease is valid, and
expiry only makes the row adoptable. A future change to the lease's meaning should be checked
against this assumption rather than around it.

**A channel port must declare whether it has an idempotency key.** The outbox's retry is safe
against Matrix because a redrive reuses the transaction id and the homeserver refuses it; against a
transport like Telegram's `sendMessage` an ambiguous timeout genuinely double-posts. That is what
brings back the "possibly duplicated" marking, which is deliberately unimplemented today because a
stable transaction id leaves it no case to fire on. Telegram also caps a message at 4096 characters,
so one neutral message can be several channel messages: "sent" is a property of a _(message,
channel)_ pair that may hold more than one remote id.

**Reprojection must preserve authored events rather than re-derive them.** Re-projecting a session's
frames cannot rebuild an event that was never in them, so a naive rebuild-and-replace would silently
delete every ownership change while the check reported green. What keeps `check_session` honest
without it having to know the category exists: an authored row names no turn, and the check reads a
turn's rows.

## 16. What is uncertain

- **Whether whole-message updates at the coalescing window feel as good as per-token streaming.**
  Following removed the refetch; the rate is still `COALESCE_WINDOW`, and nobody has compared it
  against per-token on the real page.
- **Whether "a second backend works" is worth more code before anything can check it.** No Codex
  credential exists in this cluster, so every claim in this repo about a second backend is read from
  documentation rather than measured. The cheapest fix: the tests already run a stub CLI as a real
  process (`claude_code/testing/stub_claude.py`, a `py_binary` the runner execs), and a **second stub
  speaking a deliberately different frame vocabulary** would exercise `CliBackend`, `replayable`, a
  second adapter into `conversation_events` and the status line end to end, with no credential and
  no vendor. Costed, not scheduled — and it may still not be worth its cost.

  What is genuinely in the way of a second backend is not on § 9's list: the control channel
  (`ClaudeCli` owns `initialize` and `interrupt` in Claude's `control_request`/`control_response`
  spelling) and choosing a backend per session, which the console cannot do while
  `session_runtime.py` imports `build_claude_launch` statically. Both are seams with one caller, and
  <../../runtime/x/bridge/docs/second_backend.md> is right that a registry before a second backend
  exists is a mechanism with one user.
