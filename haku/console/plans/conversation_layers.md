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
| **Conversation** | which sessions are the same thread, and which channels are attached to it — **identity, and nothing else** (§ 6)                                                                                                                                               | none today                    | the operator ends the thread                              |
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

**What the stream carries is the record, so its granularity is the record's.** A `TextDelta` is not
a row, so no subscriber gets one; a message being written arrives as its open row, re-sent. Pushing
deltas to the browser as a payload on the wake was considered and dropped: it would make one channel
a partial replica of a stream the others read, which is the duplication this primitive exists to
remove.

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
user serve exactly one room ever.

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
- **The SPA is not an attachment, and is allowed to be special.** It may open any session, and
  nothing about it is recorded. That follows from § 2's test rather than excepting it: an
  attachment row exists to hold a cursor, a cursor exists because a channel holds a copy the
  console owes work against, and a tab holds no copy. So `chat_attachment` is for copy-holding
  channels only, and "the browser is looking at this conversation" is an absence — no row to key by
  an address a tab does not have, and nothing to close when it goes away.
- **Channel state lives in Postgres, not in the room.** The watermark stays a row; `m.fully_read`
  and per-room `account_data` are not pursued. The reason given was preference for the known
  quantity — "postgres is known, state in matrix, who knows" — and it is reinforced by the
  mechanics § 3 already lists: account data is per-user with no compare-and-set while several
  replicas act as one Matrix user, and it is one position where R2.5 needs two. This does **not**
  retire reading the room; § 3's correspondence reader is how the reconciler learns what the room
  currently shows, and that is a read, not a store.
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

- **Ingress's two positions.** `matrix_sync_watermark` is a promise and `matrix_held_batch` is what
  is being read from — exactly the read-position/acknowledgement-position pair § 2 generalises, and
  the batch is acknowledged after the turn completes rather than at enqueue (R2.5). Keep as is.
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

1. **The browser joins a session instead of creating one.** No schema and no dependency on anything
   below, and it is most of what "both surfaces open on one conversation" looks like from the
   operator's side: `enqueue_prompt` has no surface check and § 8's increment route already serves
   updates, so what is missing is a chat page that opens an existing session.
2. **`conversation`, then `chat_attachment` keyed on it** (§ 6). One change, because the
   attachment's key is the whole point of the identity — building the attachment on `session_id`
   first would mean writing the re-pointing logic and then deleting it. Subsumes
   `matrix_conversation` and `sessions.room_id`.
3. **The conversation list surface.** `/chat` and `/conversations` merge
   (<session_channels.md> § 2) into one list of conversations — each showing its attached channels,
   its last activity, and whether a session is live — with "new conversation" minting the
   conversation and its first session together. Step 1 lands before this and lists sessions; the
   list query is rewritten once here, which is the price of not blocking a visible surface behind a
   migration.
4. **Record a prompt's provenance** (§ 8's missing item 3). A structured link from the transcript
   row to the channel event it came from, rather than brackets in prose. Small, additive, and what
   makes "answer the message that asked" expressible at all.
5. **Move the artifacts that are durable in the wrong layer.** The abort notice is a
   `session_outbox` row keyed by `turn_id`; § 7 settles that it is an event. This is also what gives
   a reconciler the conversation-side identity it needs to be at-least-once, which § 3 names as a
   prerequisite rather than a follow-up.
6. **Give the facts in stack frames a writer** (§ 8's missing item 4), so a notice body can be a
   pure function of the record.
7. **Record what we sent** (§ 8's missing item 2), or turn on the correspondence reader. § 7's
   ruling puts channel state in Postgres, which argues for storing the `event_id` beside the
   attachment rather than deriving it from the room every pass; the room read stays available for
   repair. This decides how idempotence works, so it wants its own PR.
8. **Matrix becomes a subscriber** (§ 2's primitive): one loop per `(channel, conversation)`,
   reading the record from its cursor instead of being handed events by the turn loop, woken by
   `session_changed` and by inbound events with the 1s poll demoted to a fallback. Here the three
   elections collapse to one, the three egress mechanisms become one difference calculation, the
   pacer's bucket stops being an estimate, and the browser stops being the only consumer that reads
   the record.
9. **Notices as spans** (§ 4), once 6 and 8 exist: one work notice per turn, one lifecycle notice
   per session, each a pure function of its span, each retired or sealed. This is Matrix's
   streaming — the granularity a channel that holds a permanent, federated copy can afford.
10. **Delete `ActivityStarted`/`ActivityCompleted` from the vocabulary** (operator, 2026-08-17):
    the `case "task_started"` arm of the projection, both dataclasses, the `session_events` bodies
    that store them, and `room_status.coarse_status`'s arm — so a status line that would have shown
    the harness's prose shows `writing`. That loss is the price of the invariant. **Keep
    `ConversationEventKind.ACTIVITY_*` and `ck_session_events_kind` as they are**: rows of those
    kinds may exist, the column is parsed rather than read as text, and a previous image still
    writes them for the length of the roll.
11. **Delete the rows and narrow the kind**, a release after 10 has converged: `DELETE FROM
session_events WHERE kind IN ('activity_started','activity_completed')`, drop the two enum
    members, narrow the CHECK — one migration, the shape `0059`'s downgrade already uses. Deleting
    the members before the rows would make reading one raise rather than degrade, and deleting the
    rows earlier is harmless but pointless: the previous image is still writing them.
12. **Audit the rest of `ConversationEvent` against the same question**: could a second backend
    produce this, or is it one provider's concept renamed? Do this before a second backend exists,
    because afterwards every answer is retrofitted to what that backend happens to emit.

13. **Delete `Usage`** (operator, 2026-08-17), for a different reason from 10: not that it fails
    the neutrality test — it passes, being a reduction to quantities every backend reports — but
    that nobody wants the feature. `Usage` leaves `TurnCompleted`, `_usage` leaves the projection,
    `TurnUsage`/`_turn_usage` leave `session_views`, `ConversationTurnView.usage` leaves the API,
    and the frontend stops rendering cost. Pure code.
14. **Unmap the `session_turns` usage columns**, a release after 13. **Check each for a server
    default first** — a `NOT NULL` column without one breaks every `INSERT` the moment it is
    unmapped, which is the trap `session_frames.partial` walked into and the reason its own
    sequence grew a step. Find that out here, not at the drop.
15. **Drop them**, a release after 14 has converged.

Steps 10–15 are a separate lane from 1–9: nothing in the layering depends on them and they do not
wait on it. **Both deletions lose something recoverable rather than something gone.** `Usage` is
read straight off the `result` frame's payload — `usage.input_tokens`, `cache_read_input_tokens`,
`total_cost_usd`, `duration_ms` — and what `ActivityStarted` recorded came off `task_started` the
same way. Both payloads stay in `session_frames`, the surface allowed to be provider-shaped, so
wanting either back is a re-fold over frames. That is what makes these cheap rather than a bet.

Steps 1, 4–6, 10 and 12 are independent of each other and of step 2; 3 and 7 depend on 2; 8 depends
on 6 and 7; 9 depends on 8; 11 depends on 10 converging. Step 12 may reorder 10 and 11 by finding
more members that fail, which is an argument for doing it early rather than a reason to wait.

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
- **Nothing is announced that is not recorded.** Every notice body is a function of rows, so there
  is no fact whose only copy is a stack frame and no notice that a SIGKILL can silently swallow.
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

Nothing has ever asked the same question of the rest. `TextDelta`, `MessageCompleted`, `Reasoning`,
`ToolCallStarted`/`Completed`, `TurnCompleted` and `Usage` read as genuinely general;
`ToolReferences`, as an arm of `ToolResultContent`, is the next one to look at, since "a result that
lists tool names" may be one provider's `tool_result` shape. That is a guess, which is the reason
the audit is its own step rather than a claim here.

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

Not gone, and deliberately: `matrix_sync_watermark` and `matrix_held_batch` (ingress's two positions
are already right), `session_events`, `session_frames`, the lease.

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

Two behaviours, both testable end to end:

- A Matrix room and a browser are open on one conversation. Either can prompt. The sandbox is
  killed mid-turn; a new session takes over; **both surfaces show the same account of what
  happened**, including that the restart happened, without either having been told twice.
- A replica is killed while a turn is streaming. Nothing in the room is orphaned or duplicated, and
  the operator is told everything they would have been told had it lived.
