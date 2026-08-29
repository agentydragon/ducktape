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
(<../channels/matrix/SPEC.md> is where the Matrix channel's own guarantees live), and an invariant
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

### The edges that are bugs

Every line here is an edge the invariant still forbids after neutral runtime supervision landed.

**A channel that knows a session.**

- The SPA's raw-frame, abort, close and provisioning inspection remain correctly session-addressed;
  prompt admission itself now uses `POST /api/conversations/{conversation_id}/messages`.

### The edge that still cannot simply be deleted

Room-binding notices — "Joined — this is now Haku's room", "invited elsewhere, still serving this
one" — are queued with nothing behind them. Provisioning, setup narration and session ending are
conversation events now; the remaining channel push can go only when its own fact has a durable
home or is deliberately retired.

## 2. The protocols in detail

### Session ↔ conversation

**Session → conversation** is the fold: `apply_frame` writes the log rows, the items they
materialise and `sessions.projected_frame_seq` in one transaction. One writer at a time, because the
lease is single — which is what lets `event_seq` be a dense counter per conversation, the premise
the increment design depends on.

**Conversation → session** is admission and re-awakening. `enqueue_prompt` accepts a prompt on a
ready session with nothing queued and refuses otherwise; a replacement session is handed the
conversation's tail as prompt text.

**Handover** is the operator's "only one session holds it at a time", and the lease is what holds
it: `authenticate_runner_connection` admits one runner connection while a lease is valid, and expiry makes the
row adoptable (§ 14). `channel_attachment`'s partial unique index is a different rule wearing the same
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
mechanisms**. The browser reads the record, and Matrix now does too for replies, live status,
sealed notices, relayed prompts and silent turns. Matrix is still split between that subscriber and
direct room pushes for attachment notices (§ 8), so one fact can still have one recoverable
projection and one process-local rendering. Under one subscription there is a
single path from the record outward, and a channel differs only in two ways:

- **How it renders what arrives.** A run of tool calls is a list in a tab and one folded notice in a
  room (§ 4). That is the channel's own decision and it belongs there, not in the stream.
- **Whether its position is durable.** A room holds its own copy, so its position is a cursor the
  console owes work against; a tab holds none, so its position is an argument to the next read.

The primitive is code: `Subscription.read()` over a `ConversationStream` in `session/subscription.py`,
where "no position given" is the `Unstarted` arm rather than a zero, and
`WS /api/conversations/{id}/follow` is the same contract over one socket — a snapshot, then the
changes, with the conversation wake as what says to read again. Matrix's
`ConversationSubscriber` consumes that primitive, one instance per attachment: it queues completed
answers in `matrix_outbox`,
folds the stream into the span lines and typing, and projects the settled rejection,
unreadable-input, relay, silence and turn-abort/failure notices, checked against the room's own
recorded copy before it sends. What is missing is not another stream but closing the last direct
path — the room-binding notices.

**The stream carries two kinds of message, and a delta is one of them** (operator, 2026-08-17).
State changes are whole rows, merged by id. A **delta** is `{item_id, append}` — carried to a
consumer holding the live socket and never a row of its own — and every consumer receives it: a room
ignores it because it cannot edit per token, a tab applies it and shows prose as the model writes
it. One mechanism with two message kinds, not a side channel.

**The item row is the resync, which is what makes it safe.** `conversation_item.text` is the fold of
that item's segments, so a subscriber that misses deltas, joins mid-message or reconnects reads the
row and is correct. No exactly-once, no ordering guarantee, no gap detection — the same property that
made whole-row replacement the right payload for state changes. And because prose is stored only as
segments, a subscriber replaying from a position never reprints what it already printed. Note what
this changes about pacing: `COALESCE_WINDOW` stops being the rate prose arrives at and bounds only
the state messages.

**v0 may ship without the delta kind** (operator, 2026-08-17). The increment already carries the
open message row whose `content` is the prose so far, so a subscriber that ignores deltas still
watches a message grow — at `COALESCE_WINDOW` granularity rather than per token. That is the
fallback if the delta kind costs more than it looks like it will. Adding the kind later changes no
schema and no stored row — only what the socket emits between coalesced increments — so v0 shipping
without it costs a follow-up, not a redesign.

**Conversation → subscriber.**

- **The address is `conversation_event.event_seq`**, dense within the conversation, so a subscriber
  reading "everything after N" can tell a gap from an end.
- **The wake carries no payload beyond its address**, and the address is the conversation:
  `pg_notify` names `conversation_id` and a conversation subscriber keys on it. Level-triggered,
  edge-scheduled — `LISTEN`/`NOTIFY` is broadcast, each replica wakes the followers it holds, and
  changes coalesce per window. The browser-facing invalidation
  (<../conversation/live_updates.py>) names the conversation too — one more subscriber, with the
  console socket as its transport.
- **The position belongs to whoever needs it, and its shape follows from what they hold.** A tab
  holds no copy that outlives it, so its position is a query parameter and the server keeps
  nothing. A room holds its own copy, so its position is a durable cursor: a position behind the
  record is work the console still owes. The test that separates them is "does this subscriber hold
  a copy?".
- **The address covers an open message.** A segment is a row, so a message being written is in the
  log as it arrives and a subscriber reading from a position sees it grow. The socket's delta kind
  is a granularity choice on top of that rather than the only way to see prose in flight.

**Channel → conversation.** A channel whose transport retains unacknowledged input needs a read
position and an acknowledgement position, and the second is a promise rather than a cursor:
`matrix_ingress_event` holds the events a prompt in the record carries, written in that prompt's
own transaction.

**The loop**, in the operator's terms — how far is the room behind the conversation, how far is the
conversation behind the room, send what can be sent both ways, otherwise wait:

```text
wake on: an inbound event, a conversation wake, a freed send slot, or the fallback timer
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
conversation has moved past. `RoomPacer.revise` already replaces a revisable subject's change that
has not gone out, and `drop` withdraws one — reconciliation per subject, done imperatively inside a
queue whose vocabulary (`Send = Callable[[], Awaitable[None]]`) cannot express it for anything
else.

## 3. The room as the channel's own store

`EventTag` (<../channels/matrix/client.py>) rides on every event the console sends: a `kind`, an
optional `conversation_id`, and — on every record-derived event, sealed notices and span lines
alike — one optional `ConversationEventSource {attachment_id, conversation_id, event_seq}`. The
source's attachment prevents a conversation rebound to another room from reusing the old room's
identity; its conversation and event position name the durable fact — for a span, the event that
opened it.

**The correspondence reader exists, and the reconciler consults it before sending.** The mirror of
ingress — only our sender, parse the tag, never a prompt — feeds `matrix_room_copy` from the
events' own `/sync` echoes; a sealed notice is projected only when no event already shows its
source, the deterministic transaction id covers just the send-to-echo window, and a duplicate that
lands inside it is redacted on observation. The standing guarantees are
<../channels/matrix/SPEC.md> § The room's own copy.

- **Redaction strips the tag**, since it lives in `content`. For a level-triggered reader that is
  the right behaviour — a retired status line's desired state is "none", and a redacted event reads
  as absence. The cost is that **the room is not an audit log**: nothing can ask it what it used to
  show, so any fact that must survive its own retirement is recorded conversation-side or not at
  all.
- **The editable copy corresponds, and its content is still not compared.** A span line's create,
  edits and seal all tag the span's opening event, so the store holds the editable copy's identity
  — replays are suppressed, duplicates repaired, an operator redaction respected. What no reader
  does yet is compare the copy's _content_ against the fold's desired body (the tag carries ids,
  never text), so an edit the homeserver lost stays stale until the next state change or takeover
  sweep; step 2 of § 9 is where a content check would live if it earns its cost.
- **The token cannot live in the room**, because it is what reads the room. So the channel keeps a
  private store whatever else moves; the only question is what else is in it.

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

**A message being written is still not edited into the room**, and the reason is now the rate budget
rather than the address: the log does name an open message's current state — its segments are rows —
so a reconciler could compare the room's copy against a fold of them. What stops it is what the room
is: permanent, federated, and re-publishing an edit in full to every client that does not render
edits, since an edit's fallback body is the new text. A tab is sent the open item whole because that
copy is cheap, idempotent and discarded when the tab closes.

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

**Sealed notices and spans have both landed.** `project_notice` stays the pure one-event fold for
prompt rejection, unreadable input and turn abort/failure — appended, source-tagged, delivered
before the cursor advances — with relayed prompts and silent turns on the same path, their bodies
read from the record. Everything else the room shows is one of two editable spans
(`channels/matrix/spans.py`): a work line per turn folding reasoning and tool calls into a bounded
activity-plus-tally, and a lifecycle line per session folding provisioning, narration and adoption,
retired at the first turn and sealed by a lease expiry. The contract is
<../channels/matrix/SPEC.md> § What the room shows while a turn runs.

### What a notice body may do

- **Read the neutral vocabulary, never a backend's frames.** The span fold
  (<../channels/matrix/spans.py>) reads the stream's neutral bodies; a notice that reached into
  Claude frames would weld the channel's rendering to one backend.
- **Stay bounded independent of the span's length.** Forty tool calls summarise to a tally and the
  one in flight, not to forty lines: a room event is permanent and federated, and an edit
  re-publishes its whole body.
- **Stay coarse by rule.** Where a tool is named, its identifier passes through verbatim; no
  per-tool copy, no mapping table.
- **Say that thinking happened, at most with its summary.** `Reasoning.summary` is the only
  renderable part, and a notice per `Reasoning` event is what the single line exists to avoid.

### What a notice does not need

**No outbox row.** Its durable form is the span it summarises plus the room's own tagged copy, so
nothing about it needs a second write on the send path. That is what keeps the rule intact — record
what happened, derive what is shown — and it is why the delivery queue can be channel-private
(§ 5): under reconciliation the queue holds no facts, only work derivable from the record.

The exception is a notice whose fact is not in the record at all; anything still on that inventory
needs a conversation-side writer before it can be projected rather than sent.

## 5. Where delivery state belongs

The outbox is the Matrix channel's own — `matrix_outbox`, keyed by the attachment, written by the
room's subscriber when it reads a message complete and drained by the channel that has a credential.
A channel that keeps no copy (the SPA) needs no queue at all, and a channel whose transport has no
idempotency key needs a different one, so a shared table would put two channels' failure domains in
one queue.

### The cursor and the outbox answer to different layers

They are easy to confuse because both are "what has gone out", so name whose contract each one is
(operator, 2026-08-17):

- **The cursor is how far the conversation has been handed to the channel implementation and
  acked by it.** It is a position in the conversation's own stream — the one thing every channel
  has, including a channel that keeps no copy. What sits on the boundary between the layers is the
  **interface**: `Cursor.position()` / `keep()`. The position itself sits wherever that subscriber's
  durability requires — a tab passes it as an argument to the next read and stores nothing, a room
  keeps it in `channel_cursor`, keyed by the attachment — the one piece of channel state the
  conversation layer keeps generic, because a position in the log is the resume contract every
  attached channel owes it and the same integer answers it for all of them.
- **The outbox is the channel's queue against the homeserver.** It lives entirely inside the Matrix
  implementation, below that boundary, and what it holds is retry state about a flaky external
  server: `attempts`, `next_attempt_at`, `last_error`, a row that stays unsent until it is not.

**A cursor cannot absorb the outbox**, and this is why both survive. A position says "everything
before here is done", which is only true when delivery is ordered and gapless; a homeserver that
refuses one reply and accepts the next breaks that, and expressing "this one failed three times and
is backing off" in a cursor is just re-deriving a queue. Equally the outbox cannot absorb the
cursor: it is the channel's private business, and the conversation layer must not have to read a
Matrix queue to know how far Matrix has got.

### A session has no frontend

This boundary is now enforced: the turn runtime records conversation facts and holds no channel
object. An attachment only selects the shared direct-chat system prompt; channel subscribers render
setup, answers, silence and live state from the conversation stream. Address and delivery state stay
inside the channel.

## 6. The conversation is identity only

`conversation(conversation_id, operator_id, created_at)` and `channel_attachment` exist, with
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
keyed to the conversation and the conversation table still holds nothing but an id.

`channel_attachment` is authoritative for which rooms the bot holds, and the rule its `user_id`
primary key enforced — one bot user, one room, ever — is gone entirely: every operator-invited
room binds beside the others, which was **the point rather than the cost** (§ 7).

## 7. Settled, and still open

**Settled by the operator, 2026-08-17.**

- **The SPA is a channel like any other, and differs on exactly one axis** (operator: "the spa
  should mostly use the same affordances/operations/protocol as the matrix channel uses"). Same
  subscription, same operations, same neutral vocabulary, same provenance pointer on the prompts
  it sends. The one difference is **whether its position is durable**, and it
  is the reason it gets no `channel_attachment` row: an attachment exists to hold a cursor, a cursor
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
  sent while the sandbox provisions waits for a session to claim it; the channel-neutral
  `SandboxAllocator` reconciles that demand (<../x/README.md>).

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

- **One bot serves many rooms, and that is what parallel sessions are.** `channel_attachment`'s partial
  unique index expresses the rule that is actually wanted: **one live conversation per address**,
  which permits a bot in many rooms at once — and `bind_room` now binds each invited room beside
  the others rather than refusing the second.

  "Only one session holds a conversation at a time" is unchanged and is a **per-conversation** rule.
  What changes is that the console now runs N of them, one per attached room. Only the operator's
  own MXID still gets Haku into a room, and silently joining a room nothing services stays ruled
  out — it is now prevented by the invite creating the thing that services it, rather than by
  refusing the invite.

  **On-demand allocation is the prerequisite already met.** One room could afford a sandbox held
  open; ten cannot, and a room nobody is talking to must not hold one. `SandboxAllocator` now buys a
  claim only for durable prompt demand, independently of the channel.

**Still open.**

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

The conversation stream and the Matrix subscriber are real, but one bound room is still served by
several mechanisms with different recovery properties.

### The channel is four mechanisms with three durabilities

| What                                             | Driven by                                                                             | Recovery today                                                                                                                                                       |
| ------------------------------------------------ | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Assistant replies                                | `ConversationSubscriber` queues `matrix_outbox`; `RoomOutboxDrain` sends              | Durable row, ordered retries, stable outbox transaction id                                                                                                           |
| Sealed notices, relayed prompts and silent turns | `ConversationSubscriber` → `project_notice`, suppressed by `matrix_room_copy`         | Durable source, post-send cursor and recorded correspondence; duplicate-safe past the transaction cache, with observed repair                                        |
| Span lines and typing                            | `ConversationSubscriber` folds `spans.LiveSpans`; `RevisionLog`/`RoomPacer` render it | Durable subject and revision; seals cursor-gated and copy-suppressed; edits and retires level-triggered, repaired by the takeover sweep; typing deliberately expires |
| Attachment narration                             | `_handle_invite` and room adoption call `_queue_notice`                               | The room-binding facts have no row; delivery is process-local                                                                                                        |

One reconciler owns each attachment: the sync leader (`MXSY`) sweeps an `AttachmentReconciler`
per live binding — that room's subscriber, outbox drain and `RoomPacer` — so every sender of a
room runs on one replica, `bound_room()` and the process-global pacer are gone, and a second
operator invite creates a second conversation and starts its reconciler beside the first.

### What is already the right shape

- **The record and subscription.** `ConversationStream`, dense `conversation_event.event_seq` and
  `Subscription.read()` are shared with the browser rather than invented for Matrix.
- **The attachment cursor.** `channel_cursor` is keyed by `channel_attachment`, advances monotonically,
  and represents work the channel still owes.
- **Replies as channel-private retry state.** `matrix_outbox` is attachment-scoped, ordered and
  deduplicated by subject.
- **A pure sealed-notice projector.** Its body is one neutral event in and Matrix prose out; the
  transport waits before the cursor advances.
- **A reconstructible span fold.** `spans.LiveSpans` derives the editable lines — create, edit,
  seal or retire, bounded bodies, durable subjects — from conversation events alone, replay-safe by
  position, without a turn-process callback.
- **The correspondence key, and its reader.** `ConversationEventSource` names attachment,
  conversation and event position; `matrix_room_copy` holds what the room shows under it and the
  sealed-notice send consults it first.
- **Cross-surface prompt provenance.** A prompt typed in the SPA already appears in Matrix with its
  origin distinguished; only its delivery path remains non-reconciling.

### What is missing

- A durable home for Matrix-only attachment narration — the room-binding notices are the last
  direct push.
- A content check of the editable copy against the fold's desired body, if a lost edit ever proves
  worth more than the next state change already repairs (§ 3).

## 9. The order

Each step is independently reviewable. The dependency order below is the channel stack; the
backend-neutral runtime/Agent-selection work in #4431 is a parallel review track and must not be
mixed into these PRs.

1. **Completed — Matrix's own copy is read** (§ 3). The own-sender `/sync` projection feeds
   `matrix_room_copy` without admitting anything as a prompt; reconciliation finds an existing
   source before sending, the deterministic transaction id covers only the send-to-echo window, and
   a duplicate that lands inside it is redacted on observation. The editable copy stays unread
   until step 2 gives it a durable subject.

2. **Completed — turn notices are spans** (§ 4). The fold (`channels/matrix/spans.py`) produces
   bounded bodies under stable turn/session subjects — the opening event's position — and closes
   each span by sealing scrollback facts and redacting spent live state; the subscriber reconciles
   it beside the sealed notices, off one cursor, with a takeover sweep for lines nothing open
   accounts for. The pure fold and the Matrix effect are tested separately.

3. **Completed — session supervision is behind the conversation.** `conversation.runtime.Runtime` owns
   global lease expiry, terminal-claim cleanup and exactly-once idle-session creation under the
   conversation row lock. Web and Matrix admit prompts by conversation; Matrix has no session
   supervisor, lifecycle latch or `MXSE` lock. The runtime identity seam from #4431 may later inform
   which session implementation is created, but no provider choice leaks into the channel.

4. **Completed — reconciliation is attachment-scoped, and rooms are served in parallel.** One
   Matrix `/sync` owner keeps the user-wide token and dispatches its room events by attachment; the
   sync leader sweeps one reconciler per live attachment
   (`channels/matrix/attachment_reconciler.py`) owning that room's conversation cursor, reply
   outbox, revisions and send budget, so an operator invite creates another conversation and
   starts another reconciler. `bound_room()` and the process-global pacer state are gone;
   `RoomPacers` addresses the budgets by attachment.

5. **Add Matrix commands.** Abort first, then new/close session semantics once step 3 makes them
   conversation operations. Commands are ingress interception, never agent tools or approval
   gestures. Use a namespace Element does not consume (for example `!haku stop`).

6. **Interlink the channels.** Matrix events link to durable console routes; the console links back
   with `matrix.to`; sessions and tool calls link both ways. This is independent of the others once
   the route names are chosen to survive permanent, federated events.

**Dependencies.** Steps 5 and 6 are independent of each other. The
channel-neutral allocator is already complete and is not a step in this plan.

**Independent runtime work.** The `provisioning` refinement (§ 10) and the read-surface work
(§ 13) are not channel dependencies. Bridge v3 already made the frame payload harness-neutral; any remaining
numbering/contract cleanup stays in that runtime stack. The
`session_runtime.py` split and its abort wait can land with #4431 or step 5 on their own merits, not
as incidental channel-reconciler edits.

## 10. `provisioning` is lossy

The status column is gone — the row stores facts and `database_schema.Session.status` derives the
vocabulary — but `provisioning` still stands for several distinct facts: claim submitted, pod
scheduled, sandbox running, runner dialled back, and the row records only the first and last. So a
session that never came up reports `failed` plus free text, where the operator wants "the claim was
never satisfied" told apart from "the sandbox ran and the runner never dialled". The refinement is
more fact timestamps (`claim_submitted_at`, `sandbox_ready_at`) each deriving a finer member,
shipped derivation-first per the decision-value roll rule.

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
frame whole, deliberately unclipped, because `conversation_item` is a lossy projection of it and
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
not establish that a field has no reader when a projection renames it at a layer boundary. The
`unreadable` count an agent used to receive changed name on its way out — `Transcript.unreadable`,
`TranscriptSlice.unreadable`, `TranscriptPage.unreadable` — so grepping any one spelling found
tests and little else. That chain is gone with the fold-backed read; `unprojected` reaches no
surface either, and the adapter's count of frame classes it could not map is asserted by the
capture tests and shown to nobody.

**The runtime seam now enforces the rule.** `RuntimeAdapter.turn_handler()` returns a provider-owned,
typed stateful reducer, and each native frame crosses back into generic Console code only as neutral
`FrameEffects`. The turn loop, adoption and reprojection all drive that interface; no generic layer
selects a branch from the native JSON. Exact payloads remain separately addressable in the raw-frame
inspector for forensic review.

### What still disappears

`matrix_conversation`, `sessions.surface` and `sessions.status` have already been removed; they
are no longer plan items. Not gone, and deliberately:
`matrix_sync_watermark`, `conversation_event`, `session_frames` and the lease. `matrix_outbox` stays
too: it is the channel's retry state against a flaky homeserver, which is not derivable from the
record (§ 5).

**Mechanisms**

- `RoomPacer` as a queue of opaque callables — a budget the reconciler spends, not a deque of
  closures it cannot inspect or squash.

### How to tell it is finished

Six behaviours. The first four are the surfaces working; the last two are what proves the
architecture rather than the features, and each is the acceptance test for the thing that forced it.

1. **The Matrix surface still works.** `channels/matrix/test_fullstack_e2e.py` against real
   Synapse stays green throughout. A regression gate, not a new test.
2. **The web surface works**: one merged surface lists conversations, creates one, sends, aborts,
   and shows a transcript.
3. **The web surface streams per token.** A message being written grows in place at
   `COALESCE_WINDOW` granularity today, which is the weaker form; the delta kind § 2 designs is what
   tightens the assertion.
4. **Both surfaces show the operator's own prompts, wherever they were sent from** (§ 4). A prompt
   typed in the SPA appears in the room; one sent from the room appears in the tab; neither appears
   twice on the surface it was typed on — which the prompt item's `origin` is what decides.
5. **One conversation, two surfaces.** A room and a browser both open, either can prompt, both show
   the same account. This is what the conversation entity is for, so it is its test.
6. **A session replacement is invisible to both.** The sandbox is killed mid-turn, a new session
   takes over, and both surfaces show the restart without either being told twice.

**Write these per step, not at the end.** A failing test cannot land on `devel`, so each step's PR
carries the test that proves its own part. Correspondence and spans add cache-expiry, duplicate and
create/edit/retire cases; attachment-scoped delivery adds a real two-room end-to-end case; neutral
session supervision adds replacement during a turn without either channel being told twice.

**Encode the invariants as tests too**, because each is false today and would otherwise creep back
silently:

- `session_store` contains no reference to `room_id`, and nothing under `channels/` names
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

## 13. The read surfaces, and the line between them

The console reads its session corpus through two surfaces that answer nearly the same questions off
the same tables — REST for the browser, `haku_conversations` over MCP for agents. The two share one
read model (`conversation/reads.py`, folded once in `conversation/item_reads.py`); what stays two is the
transport and its envelope.

The boundary, stated as a rule:

- **REST owns every read whose answer depends on _who is asking_** — the inventory, the interpreted
  transcript, and the frame inspector. An in-process MCP server is handed a **credential, never a
  caller**, so operator scoping is not expressible there today.
- **MCP owns every read whose answer is the same for everybody with the authority to ask it** —
  reflection and semantic recall (`haku_index.search`), which has no REST twin and should never grow
  one.
- **`/api/events/ws` says _what changed_; the read surface says _what it is_.** A
  `ConversationChangedEvent {conversation_id}` is an invalidation, not a payload.
- **Mutations, the WebSocket, and anything the service worker touches stay REST**, always.

One thing is left, and it needs no transport decision:

- **Make the reader reflectable.** `build_schema_servers()` leaves `conversations` and `index`
  unset, so the conversation tools are absent from both generated catalogs. Passing the same inert
  object as `conversations=` (and `index=`) registers them for reflection: one file, zero production
  behaviour. Every JSON Schema keyword those models produce is already in the reviewed
  `_FRONTEND_SCHEMA_KEYWORDS` allowlist, so this should generate without adapter work — and if it
  does not, that is a cheap and decisive answer.

**Not planned: moving `/api/conversations*` onto MCP.** Revisit only when **both** hold: an in-process
server can be handed the acting principal (a fourth `InProcessCredentialKind`), and the tier decision
function from <../../plans/information_trust_tiers.md> exists at the one console call site it is
meant to live at. Until then, moving an operator-scoped browser read onto a deliberately unscoped
tool either widens what the console shows or forces scoping into the surface designed not to have
it. Three costs a plan should state rather than discover: the browser's argument shape would be
downstream of an agent policy file (`_is_passthrough` reads the auto-approval registry, so dropping
a tool from Haku's policy silently makes the console page need the `{input, rationale}` envelope);
the MCP surface's prose is written for an LLM reader; and a page moving to MCP trades a typed
404/409 for a joined-text error blob and loses the generated `paths` typing.

## 14. The standing rules

Not scheduled items — constraints that govern everything above.

**Every outbound channel write is recorded first and sent from the record** (operator, 2026-08-16:
_no events should be written directly into Matrix without going through our database, because
Matrix is just one of pluggable backends — channels_). A write that goes straight to the homeserver
is invisible to every other channel, unrecoverable across a crash, and unprojectable. The test of
compliance is not "does it work" but "could Telegram show it" — which makes the easy-to-forget
writes the interesting ones: typing indicators, edits, redactions, invites, and the console's own
notices. The current channel implementation is the inventory.

**The projector is single-writer per session.** The lease gives that, and it is the reason none of
this needs the fold to be re-runnable. An expired lease means unowned rather than dead, but the
property still holds: `authenticate_runner_connection` admits one holder at a time while a lease is valid, and
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

## 15. What is uncertain

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
  <../../runner/docs/second_backend.md> is right that a registry before a second backend
  exists is a mechanism with one user.
