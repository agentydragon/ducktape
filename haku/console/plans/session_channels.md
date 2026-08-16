# Matrix and the console as two channels onto one session

**Status: proposal.** Direction set by the operator, 2026-08-15: drive Matrix and the web
frontend toward being **different messaging channels** onto the same session, each able to do
broadly what the other can. Today they are two half-surfaces — Matrix can hold a conversation
but cannot start or stop one; the console can start and stop one but cannot show what is
happening — and the console's own half is split across two pages that are the same object.

This supersedes nothing; it is the frame the pieces below belong to.

## Where the parity actually stands

| Capability            | Matrix                         | Console                        |
| --------------------- | ------------------------------ | ------------------------------ |
| Read the transcript   | native                         | yes, read-only                 |
| Live updates          | native                         | yes — invalidate, then refetch |
| Send a prompt         | yes                            | **only on `/chat`**            |
| Start a session       | no — the supervisor owns it    | yes, on `/chat`                |
| Abort a turn          | **no**                         | yes, on `/chat`                |
| Close a session       | no                             | yes, on `/chat`                |
| A turn is in progress | typing indicator + status line | **no**                         |
| Lifecycle transitions | `m.notice`, unrecorded         | **no**                         |
| Bootstrap narration   | `m.notice`, one line each      | yes, at the transcript's head  |
| Formatted replies     | yes (R11.7)                    | yes (Markdown renderer)        |

**Parity is not symmetry.** Some gaps are load-bearing and stay: Matrix does not start sessions
because the supervisor owns provisioning (R3.1, R3.2), and the console does not bind or unbind
rooms (R3.6, R3.6c). The goal is that no channel is missing something for **no reason** — which
is what most of the bold cells above are.

## 1. A candidate model: a channel is reconciled against the session

**Not a requirement** (operator, 2026-08-15) — a candidate, and the thing it is a candidate _for_
is worth stating, because it decides whether it earns its cost. **The session's record is the
truth; a channel is driven toward agreement with it.** Not "send this event when it happens" but
"make this channel show what this session has". The unit is a loop per `(channel, session)`, and
its state is one cursor: how far this attachment has been brought up to date.

**The layer model this candidate sits in is <conversation_layers.md>** — session, conversation and
channel, the operator's cardinality over them, the subscription contract in both directions, and how
a turn's tool calls and thinking project into a room as notices. It also answers, against this
section, where the delivery queue belongs: to the Matrix channel, not to the shared schema.

**What it is for: plurality on both axes.** Different chat backends (web, Matrix, whatever comes
next) and different AI backends (an OpenAI-compatible endpoint alongside Claude). Those are the
two seams <../../plans/chat_runtime_cleanup.md> § stage 7 already names, and this is the frontend
one done convergently rather than by fan-out. Together they are what makes N backends by M
channels additive instead of multiplicative — each channel written once against the record, each
backend written once against the transcript.

**The constraint that makes the pair work, and the one way to get it wrong: the reconciler must
read only the backend-neutral record.** The transcript and the session events, never the rollout.
Stage 7 already settles that the rollout is one protocol's wire and cannot be what a second
backend shares ("transcript primary, rollout per-backend evidence"); a reconciler that reached
into Claude frames to decide what a room should show would weld the channel layer to the backend,
which is the exact coupling both seams exist to remove. Two consequences follow, and they are the
honest price: what a channel can show is the intersection of what every backend reports, so cost
and usage on a `TurnCompleted` are neutral while thinking blocks and tool-boundary steering are
not — and a channel wanting the second kind must ask the backend, not the reconciler.

That is a different primitive from a delivery queue, and it buys three things:

- **Repair is the normal path rather than the exception.** A send lost to a replica dying between
  recording and delivering, a 429 that outlasted its retries, a room that was unreachable for a
  minute — all become "the next pass notices this channel is behind", instead of each needing its
  own guarantee bolted onto a queue.
- **Write-back stops being a special case.** An operator message typed into the console is a
  transcript row the room does not have. That is _the same divergence_ as an undelivered reply, so
  the reconciler posts it — as a Haku-originated notice, since `@haku` is the only credential the
  console holds. No separate post-on-send path, and no enqueue/post ordering judgment to get
  right (§5).
- **It subsumes the room outbox rather than competing with it.**
  <../../plans/chat_runtime_projection.md> § stage 5 turns the Matrix pacer's deque into rows; that
  is the push half, delivering promptly. The reconciler is the convergence half, delivering
  eventually and at most once. Build the cursor first and the outbox becomes the fast path that
  advances it.

**The distinction that says which channels need a loop at all: does the channel hold its own copy?**
Matrix does — a room is a separate store with its own history, so it can drift and must be
reconciled. The console does not: its client reads the record, so it converges by refetching and
needs only to be told _when_ (§4). One idea at two ends, and worth stating so nobody builds a
reconciler for the console out of a taste for symmetry.

What it needs, concretely:

- **A per-attachment cursor**, on `chat_attachment`, which exists as of migration `0064` —
  `(attachment_id, conversation_id, surface, address, attached_at, detached_at)`, keyed on the
  conversation rather than on the session so that replacing a session leaves the attachment alone.
  The cursor column is what this section adds to it.
- **A record of what the channel already put in its copy**, which `chat_delivery` is as of
  migration `0067`: `(attachment_id, subject, sent_ref, sent_at, retired_at)`, one live row per
  subject. `subject` is what the channel decided to show and `sent_ref` is where it put it, both
  opaque outside that channel — because how much of the record one event shows is a rendering
  decision, and a conversation-layer key for it would put that decision in shared schema.
- **A per-channel projection**, because not every recorded event belongs on every channel — and
  "nothing to show" is a valid answer that still advances the cursor.

Two gotchas, both about the end that is not a database:

- **A room is append-only and federated, so an over-eager reconciler double-posts permanently.**
  The existing guard fits, and fits _better_ here than under a queue: `EventTag.transaction_id`
  derived from the transcript row is stable across processes and retries, so two reconcilers
  reaching the same conclusion still produce one event. Its limit is Synapse's 30–60 minute dedup
  window (<../docs/chat_runtime_facts.md>); past that the stored correspondence is the only guard,
  which is the argument for writing it in the same transaction that records the send rather than
  after it — what `RoomOutbox.mark_sent` does with `sent_at` and the `chat_delivery` row.
- **Convergence is not available at every edge, and the line moved.** A typing indicator has no
  cheap "what does the room currently show" to compare against, and stays out of the loop — it is
  live-state rendering driven by the turn (§3). The status line does have one now: its event id is
  a `chat_delivery` row, so editing and redacting it are level-triggered against the record rather
  than against whichever process posted it. That is what the sync service reads instead of the
  instance attribute it held, which is why a replica adopting a session no longer posts a second
  line beside its predecessor's.

## 2. One surface, not two pages — done, and it lists conversations rather than sessions

`/_console/chat` created and drove one session over SSE; `/_console/conversations` listed every
session and rendered a read-only transcript from one REST fetch. Same object, two routes, two nav
entries, two data paths, two view models over the same rows.

**Merged, keyed by the conversation rather than the session.** The list is conversations, keyset-
paged because a conversation never ends (<conversation_layers.md> § 7); the detail carries the
thread's attachments, the current session's transcript, the composer, abort and close.
`ClaudeChatPage` is gone.

**The live-update path is what the merge cost, and the cost was taken.** `/chat` streamed
token-by-token over SSE; the merged view refetches on a coalesced notification (§4), which reads
as near-live but is not per-token. `/api/sessions/{id}/stream` is gone with the page that read it
— this section previously said to keep it until a coalesced refetch had proven itself, and #4282
is where it did. Retiring it is what makes the `asyncio.wait` abort dance in `_run_turn`
removable, which is still deferred.

**The delta path itself did not go away**, and nothing here should be read as saying it did.
`RolloutRecorder` records deltas with no exclusions because the fold needs a log with no hole
(<../../plans/chat_runtime_projection.md> § stage 1), and `update_partial_frame` is what makes an
interrupted turn's half-written answer survive (R5.5b). Only the SSE transport went. Per-token
streaming to a tab comes back as the subscription's delta kind (<conversation_layers.md> § 2).

## 3. Session-level events belong in the database, not only in the room

Four things get posted to the room today. They are not one kind of thing, and the difference
decides which become rows:

| What                    | Written by                                             | Recorded today                    |
| ----------------------- | ------------------------------------------------------ | --------------------------------- |
| Lifecycle transitions   | `_SessionStatusAnnouncer` (channels/matrix/session.py) | **nowhere**                       |
| Bootstrap narration     | `_progress_reporter` → `ChatFrontend.report`           | **already** — `setup_output` rows |
| The in-turn status line | `_TurnStatus` → `show_status`/`clear_status`           | no, and deliberately              |
| The typing indicator    | `_TurnStatus` → `set_typing`                           | no, and deliberately              |

**Record what happened; derive what is being shown.** The status line and the typing indicator
are Matrix _renderings of live state_ — R6.3 already says status is a coarse state the console
derives from the frame stream it is consuming anyway. The console channel should derive the same
state from the same source and render it its own way (an activity line, a spinner), not replay
Matrix's edited-message trick. Recording the status text would be recording a rendering, and it
is the one mistake this section exists to prevent. It is also why §1's loop does not own them.

**Lifecycle is recorded — and this section was wrong about where.** It argued from
`_setup_output_frame`'s precedent: a console-authored session event lives in the frame log rather
than a table of its own, because the question a reader asks is "what happened in this session, in
order", and for a session that died before the CLI produced anything the answer is entirely there.
So lifecycle events would become frame-log rows under their own bridge-side `kind`, not a new
table. The operator settled it the other way (2026-08-16):

> i think the right thing would be: frames only come from actual runner<->console communication.
> events like "session taken over by this replica of console" are not that. so they would probably
> arrive as a different sort of event.

**The frame log is the record of runner↔console traffic and nothing else**, and that property is
worth more than the convenience traded for it above. A lease changing hands crosses no wire, so a
frame-log row for it is an envelope invented to fit, and every reader that treats `session_frames`
as evidence of what was said would have to learn which rows are not that.

So lifecycle events are `session_events` rows on the `authored` arm
(<../../plans/chat_runtime_projection.md> § stage 4). The ordering argument above survives the
move intact: that table already carries the conversation in sequence, so a session event beside it
is one ordered log and §1's cursor is a position in it rather than a join across several.
Bootstrap narration keeps its frame and is not the same case — a `SetupOutput` envelope **is**
runner→console traffic, which is the distinction this section missed.

Two things fall out, both good:

- **The console channel renders bootstrap narration**, from rows that already existed —
  `ConversationSessionView.narration`, drawn at the head of the transcript on the conversations
  detail page. It was the cheapest single item in this plan, and it is done.
- **Announcement dedup stops being per-process.** `_SessionStatusAnnouncer._last_announced` is a
  local, so a leader handover re-announces the current status — legible in a room, but it also
  means there is no durable answer to "when did this session become `failed`". Under §1 the cursor
  _is_ the record of having announced it, and the per-process local goes.

**Vocabulary consequence.** `RoomEventKind` (`reply`, `status`, `narration`, `lifecycle`,
`rejected`, `room`, `unreadable`) is Matrix-side today — what the console meant by an event _it
sent to a room_. Under this design `lifecycle` and `narration` become channel-neutral session
events and the Matrix tag derives from them; `status` and `room` stay Matrix's own, because they
describe that channel's rendering. `rejected` and `unreadable` are already the halfway case: the
fact is an `AuthoredEventKind` row and the tag names the room's rendering of it. Do not promote
the whole enum.

## 4. Live updates, over the socket the shell already holds — built; the increment is next

**Build on `/api/events/ws`, not a second SSE endpoint.** The shell holds exactly one per tab, and
`frontend/console_events.ts`'s `useConsoleEvents` already provides the whole client half:
reconnect with backoff, a `LiveStatus` for the rail, and a callback that fires on mount, on every
event, **and again on reconnect** — precisely the "read once, then read again whenever something
might have changed" contract this wants.

**Send a notification, not a payload.** A `SessionChangedEvent {session_id}` in the `ConsoleEvent`
union; the page refetches. This is the shape `ToolCallsChangedEvent` already established, and it
is what §1 means by "the console converges by refetching": a refetch is idempotent where a delta
stream must be replayed, so a reconnect that missed events entirely still lands correct.

Four things to get right, in increasing order of how easy they are to miss:

- **No second Postgres channel, and no second `NOTIFY`.** `LISTEN` is broadcast, so every
  replica's `SessionNotifications` already receives every `session_events` event. The fan-out is local:
  a replica turns the chat events it already receives into sends on the console-event sockets
  **it** holds. Routing this through `ConsoleEventHub.broadcast` (which relays over its own
  channel) would notify twice for one update and deliver to every replica twice.
- **Coalesce, and treat that as load-bearing.** `SessionEventKind.UPDATE` fires **per stream delta**
  — hundreds per turn. One frame per delta, each triggering a full-transcript refetch, is the
  O(session)-per-token-batch cost <../../plans/chat_runtime_cleanup.md> § Anytime already flags on
  the SSE path, now paid once per open tab. Debounce per `session_id` in the fan-out.
- **Operator scoping needs a lookup, and it must not be per event.** `SessionEvent` is
  `{kind, session_id}` with no operator on it, while the hub delivers per `operator_id`. Resolve
  session → operator once per coalesce window and cache it — a session's owner does not change.
  Do **not** reach for "just add `operator_id` to `SessionEvent`" without pricing it: the payload is
  a wire contract across a `maxUnavailable: 0` roll, so widening it is expand/contract over two
  releases (<../x/README.md> § the wake channel).
- **The list wants the same event.** An inventory showing `message_count` and `updated_at` is
  stale for the same reason a detail view is.

**Done when** an open session shows a Matrix turn arriving without a reload, and there is no
polling timer anywhere in the page.

**Built as** `x/session_live_updates.py` plus `SessionChangedEvent`, with all four of the above
kept: no second channel (`SessionNotifications.watch` hands the fan-out what this replica already
hears, and `ConsoleEventHub.deliver_locally` sends without republishing), a 500ms coalescing
window per session, an owner lookup resolved once per session and cached, and the conversations
list on the same event as the detail. The SPA chat page's SSE stream is untouched — §2 retires it,
not this.

### Streaming the increment — scheduled, and designed here

**The operator chose to build this before §2's merge** (2026-08-16, <../../plans/next_month.md>
§ C): the increment first, the merge onto it, so the surviving surface never reads worse than
`/chat` does today. This subsection is that design.

What the refetch costs is the whole conversation per invalidation, per open tab
(<../../plans/chat_runtime_cleanup.md> § Anytime). What an increment removes is the **history** —
the part that grows without bound. It does not remove the notification rate, and it does not
restore per-token streaming; both are priced below.

#### The position is the page's, not a row

Both plans tie "per-consumer position" to `chat_attachment` (§1, and
<../../plans/chat_runtime_cleanup.md> § Stage 7 step 2).
**For a browser that is the wrong shape**, and the difference is not scale but what the cursor
means. `chat_attachment`'s cursor is a **delivery obligation**: the room holds its own copy, so a
position behind the record is work the console still owes and must remember across a crash — that
is the entire reason §1 exists. A tab holds no copy that outlives it. Its position is a **read
cursor**, the same kind of thing as the `next_cursor` on `GET /api/tool-calls`: an argument to the
next read, meaningful only to the caller holding it.

Persisting it breaks in three separate ways, and none is a detail:

- **There is no address to key it by.** `chat_attachment` is `(surface, address)` with a partial
  unique index on the attached rows. A tab has no address; minting one (a client-generated id in
  storage) invents durable identity for something that has none, and the unique index then either
  forbids a second tab on one session — a normal thing to do — or means nothing.
- **Nothing closes the row.** A browser does not reliably announce that it is gone, so every
  abandoned tab leaves a cursor claiming rows are still owed to it, and the reconciler §1
  describes would be repairing consumers that no longer exist.
- **It makes the read path a write path.** A durable cursor is worth nothing unless it advances in
  the transaction that delivers, so each event to each tab becomes an `UPDATE` — on the session's
  own tables, contending with the turn loop's writes. The refetch it replaces costs zero writes.

So the position is a **query parameter and nothing else**. The server holds no per-consumer state
at all — not even the in-memory `last_event_seq` this was first sketched as, because
`/api/events/ws` is one socket per _tab_ carrying every session, not one per session, so there is
nothing for a per-session position to live on. §1's own criterion — does the channel hold its own
copy? — already gives this answer: the console converges by reading the record, so it needs to be
told _when_, not delivered _to_. Reading a suffix of the record instead of all of it does not move
it across that line.

#### The address does not cover the transcript, and that is the real precondition

<../../plans/next_month.md> § Not now calls the preconditions half met — the ordered stream and its
address exist, the position does not. Reading the code, the stream itself is the part that is
short: **two things a transcript shows are not in `session_events`.**

- **The operator's own prompt.** `enqueue_prompt` writes a `session_messages` row and no event;
  `x/session_events.py`'s `row()` maps only agent-side events. So `event_seq` is not a cursor over
  the messages a transcript renders, and an increment addressed by it would silently drop the
  operator's own question.
- **The message currently streaming.** A `TextDelta` is deliberately not a row — the completed
  message carries the prose whole — so an open message is invisible in the log until
  `message_completed`, while `session_messages.content` is mutated in place per delta.

Two consequences, and the first is a dependency worth naming:

- **The `authored` arm gets its first writer, and it is uncontroversial.** An operator prompt is
  conversation, not lifecycle, so recording it as a `session_events` row settles nothing about the
  argument <../../plans/next*month.md> § 3 is blocked on — §3's claim is about lifecycle. It is
  `authored` because at enqueue time it has no frame; `set_message_source_frames` later points the
  \_message* at the frame it went out as, and the event stays authored.
- **The open message is read whole, every time.** One row, bounded by one message rather than by
  the transcript, found by the existing `uq_session_turns_open` index. It is the one thing the
  cursor cannot address, and sending it whole is already the increment.

#### What is sent is changed messages, not events

The obvious shape — ship the `session_events` rows after N and let the page apply them — is the
wrong one. `x/session_views.message_view` is where events become a message today; a second fold in
TypeScript would be one meaning maintained in two languages, kept in step across a roll where the
browser is a release behind the server. Worse, applying events demands exactly-once and in-order
delivery, while **replacing whole message rows demands neither**: a merge keyed on `message_id` is
idempotent, so a duplicate costs nothing and a re-read from an older position is always correct.
That is what makes every recovery path below trivial instead of a protocol.

`event_seq` stays the **address** — what identifies a position — while a changed message is the
**payload**. Those are different jobs and this is the design's one substantive choice.

#### The read surface

A new route beside the existing read, not a parameter on it: `GET
/api/conversations/{id}/changes?after={event_seq}` returning `{watermark, messages, turns,
status}`. `messages` there means "the ones that moved", which is a different field from the detail
view's "all of them" — overloading one name with both is how a client ends up rendering an
increment as a transcript.

- **`watermark`** is the session's highest `event_seq` at read time, and the page's next `after`.
  The detail read gains the same field, so a freshly opened transcript is self-addressing.
- **Reconnect needs nothing new.** `useConsoleEvents` already re-fires its callback on reconnect
  and on a bounded timer; the page reads changes from the position it still holds. There is no
  subscribe step to redo and no server state to re-establish.
- **A gap cannot be detected and must not be relied on.** `session_events.event_seq` is a global
  `Identity` sequence, so consecutive rows of one session are not contiguous and holes are normal.
  Correctness comes from every read being "everything after N", never "the next one after N".
- **A position older than the log is `410 Gone`**, and the page falls back to the full read.
  Nothing prunes `session_events` today — rows leave only with their session (`ON DELETE CASCADE`)
  or by <../../plans/legacy_purge.md> — but "no rows after N" and "N is before this log begins" are
  different answers and only one of them is safe to render.
- **The whole-conversation read stays**, in three roles: the opening snapshot, the recovery path
  above, and the inventory the list page reads (which is a different query and stays a refetch —
  it is bounded by session count, not by transcript length).

**One premise to re-check later: a single writer per session.** A row committed with a lower
`event_seq` after a higher one was already read would be skipped permanently. The session lease
makes that impossible today — one replica runs the turn loop and writes in frame order. Console-origin
events written by a replica that does _not_ hold the lease (§3's lifecycle, a lease changing hands)
break it, and would want a lag-bounded watermark rather than the newest row.

#### `session_changed` carries nothing new

Not an `event_seq`, not a range. A tab showing the session must read anyway whenever a turn is open,
since the streaming message advances no event; a tab showing another session already routes on
`session_id`; and the page holds the only position that matters. Putting a position on the wire
would state it in a second place and invite a client to treat the socket as authoritative about it,
which is exactly the line `SessionChangedEvent` holds today. It also keeps this change free of the
expand/contract cost §4's fourth bullet prices for widening a cross-replica payload.

#### Coalescing survives; its justification changes

The **rate is unchanged** — `SessionEventKind.UPDATE` still fires per delta, hundreds per turn, and
the fan-out still absorbs them. What falls is the cost of a flush: from one whole transcript per tab
to one open message plus whatever completed inside the window. So `COALESCE_WINDOW` stops being the
safety valve its comment describes and becomes a **latency knob** — its floor is now a round trip
rather than an O(session) read, and shortening it to make prose read more live becomes a defensible
change rather than a dangerous one. Do not shorten it in the same PR: the reason to keep 500 ms is
that nothing has measured the alternative.

#### What this does not solve

- **Not per-token streaming.** Prose still arrives one whole message at a time, at the coalescing
  window's rate. Per-delta increments do exist durably — `stream_event` frames in `session_frames`,
  addressed by `frame_seq` — but folding them client-side is one backend's wire interpreted in the
  browser, which is what the neutral layer exists to prevent. If per-token ever matters more than
  that, it is a separate argument, not this design extended.
- **Nothing for Matrix.** A channel holding its own copy still needs §1's durable cursor; this
  deliberately does not unify the two, and the section above is why.
- **`/api/sessions/{id}/stream` and `claude_chat_page.tsx` are untouched.** They keep working
  unchanged until §2 deletes them; retrofitting the increment onto a route scheduled for removal
  would be building a second consumer for it. What this changes is §2's price: at the merge the
  surviving surface already streams increments, so the regression §2 was framed as accepting is
  half-second whole-message updates rather than a whole-transcript refetch.
- **The list page still refetches**, and the second event category (§3, and
  <../../plans/next_month.md> § 3) is not built by this.

## 5. Sending from the console

### Into an SPA session

Nearly free: `POST /api/sessions/{id}/messages` exists and already does the durable thing
(`enqueue_prompt` writes the transcript row and the `session_prompts` row in one transaction).
One honest limitation and one route decision:

- **`enqueue_prompt` refuses while a turn is open or a prompt is queued**, with a 409. That is
  deliberate — admission asks the turn, and accepting mid-turn would be the fold-into-turn feature
  arriving by accident. The composer is **disabled during a turn** and says why; it is not a
  queue. Mid-turn steering (<../../plans/chat_runtime_cleanup.md> § Later) is what relaxes this.
- **One send route, dispatching on the session's channel** — not the SPA's route for SPA sessions
  and a second one for Matrix. That entry point is the `ChatFrontend` port §6 corrects.

### Into a Matrix session — lower priority

**The problem.** The console holds one Matrix credential, `@haku`'s (R5.1). It cannot post as the
operator's MXID, so a console-originated message either does not appear in the room at all, or
appears under Haku's account.

**Not posting it is ruled out**, because two readers would then see half a conversation: the
operator's own Element, and any room read tool (R11.3) when it lands. Re-awakening used to be the
third and is not any more — it reads the transcript (`channels/matrix/session.py`'s `RoomTranscript`), where a
console-originated prompt is a row like any other whether or not the relay ever posted.

**So: a relay message.** `@haku` posts the operator's text under a new `RoomEventKind` — `relay` —
tagged in `works.allegedly.haku` like every other console-authored event, and rendered so the room
states its true provenance: written by the operator, delivered by Haku's account because theirs is
not the console's to speak with.

**Under §1 the send does not post anything.** It enqueues the prompt, in one transaction, and
stops. The room is then behind the transcript by one message, which is a divergence the reconciler
already exists to close — so it posts the relay on its next pass. This is what the model buys
here, and it is worth being explicit about what it deletes: there is no "enqueue then post" order
to choose, no partial failure where one landed and the other did not, and no bespoke retry. A
console send and a dropped reply are repaired by the same code.

What remains to get right:

- **Ingress needs no change at all.** R1.5 excludes Haku's own sender from input, so a relay
  cannot loop back and be answered twice. This is the one place where posting under `@haku` is an
  advantage rather than a compromise.
- **Provenance in the prompt, without inventing an event id.** R2.4 gives each batched message a
  sender, timestamp, `event_id` and thread root. A console-originated one has a _stronger_
  identity than any of them — an authenticated operator session, not the MXID mapping of R9.3 —
  and **no event id yet**, because under §1 the relay posts strictly after the prompt is enqueued
  and may post after the turn has started. Say it came from the console and carry the transcript
  message id. R11.4's "IDs are given, not guessed" cuts both ways: an absent id is honest, a
  fabricated one is not, and the turn must never wait on the room to obtain one.
- **The same 409, with less margin.** Matrix ingress absorbs a refusal by not advancing the sync
  watermark and letting the homeserver redeliver. A console send has no homeserver behind it, so
  a refusal must reach the operator rather than be swallowed.
- **Plain text, for now.** Replies carry `formatted_body` (R11.7) because the agent writes
  Markdown; the operator writes into a textarea, so a plain body is honest and rendering is a
  later choice.

**Rejected, and priced already:** giving the console the operator's Matrix credential, and an
appservice with a puppet MXID. The first breaks R5.1's single-holder property for a send button;
the second reverses R1.1's whole design note for one.

## 6. Non-message actions in the room, and links between the channels

### Slash commands are how Matrix gets the actions the console has

The parity table's "no" column for Matrix — abort, new session, close — is one missing
affordance, not three: a way for a room message to mean something other than "talk to Haku".
Slash commands are the natural form, and they settle the abort question this plan previously left
open by generalizing it.

**They are ingress interception, not an agent tool.** A command is recognized and consumed by the
harness before batching, so it never reaches the agent as a prompt. That keeps the split
matrix_chat_runtime.md's non-goals draw — the harness owns ingress; agent tools are write-side
extras — and leaves R5.4's read-only tool surface untouched. R2.7 already carves out the room for
it: control messages flush immediately rather than waiting out the debounce.

Authorization needs nothing new. It is a DM (R3.5), the sender maps to an operator identity
(R9.3), and an unmapped sender gains no authority (R8.5). What must not follow is approvals: a
slash command is an operator gesture against the _session_, and R9.5's "a Matrix message is never
consent for a tool call" holds regardless of the leading character.

**Gotcha, and it is the kind found only after building: Element eats slash commands.**
`/me`, `/html`, `/plain`, `/join`, `/invite`, `/op` and friends are client-side and never reach
the room, and an unrecognized `/foo` produces a client-side error rather than a sent message — so
the operator's command silently never arrives. Verify against the client before choosing a
namespace, and prefer a prefix Element does not claim (`!haku stop`) over gambling that a given
verb is free.

### A room-level status pointing at the web session

R7.2 already puts the session ID in the room on startup and rotation. Now that a session has a
page, **make it a link** — that is the whole of the cheap version, it needs no capability the
console lacks, and it lands in the message the operator would scroll to anyway.

The richer version, a persistent room-level status, is worth wanting and is not free:

- **`m.room.topic` and `m.room.pinned_events` are state events, gated by power level.** The
  operator creates the DM (R3.6), so they hold PL 100 and `@haku` joins at the default 0 —
  meaning the console most likely **cannot** set either today. `channels/matrix/client.py` sends no state
  events at all, so this is unbuilt capability rather than a switch. The fix is the operator
  granting Haku PL 50 in Element, which is a manual gesture with no place in R3.6b's
  adopt-from-traffic path. Check the actual power levels before designing on top of this.
- **Presence (`m.presence` `status_msg`) is per-user and global**, not per-room, and Synapse
  deployments commonly disable it. It is also the wrong scope the moment R3.6a's [later] shape
  arrives — one room per `(operator, agent)` — since one status would have to describe several.

So: link in the notice now; topic as a follow-up gated on a power-level check.

### Interlinking, now that there is a frontend to link to

Worth doing broadly and in both directions — room notice → console session, console session →
the room (a `matrix.to` permalink, which `channels/matrix/client.py` already builds for read results,
R11.5), session → its tool calls, tool call → the session that made it. The last of those has its
precedent already: `/_console/tool-calls/<tc_…>` opens the embed view with the approvals drawer on
that exact call, so "a URL names one thing and opens it" is a pattern to copy rather than to invent.

**One ordering constraint, and it is not obvious: settle the session route before minting links
into the room.** A Matrix event is permanent and federated — the console cannot take back a URL
it posted. §2 merges `/chat` and `/conversations` into one sessions surface, which is exactly the
change that would strand every link posted under the old route. Do the merge first, or post links
under a route chosen to survive it.

## 7. What this corrects in the existing plans

Two places state that the SPA needs nothing from the room-facing port. That premise was true of a
surface whose client only read message rows; it is not true of a channel:

- **`ChatFrontend`'s docstring** — "The SPA needs none of this — its client reads the message rows
  over SSE, so a finished turn is delivered by being written down." Under §3 the console channel
  wants lifecycle and narration, and under §4 it wants to be told when.
- **<../../plans/chat_runtime_cleanup.md> § The frontend seam** — "a null implementation for the
  SPA, which needs none of it". The `ChatFrontend` port with no address parameter is exactly
  right and survives unchanged; **the null implementation does not.** Both channels implement it,
  and that step's `chat_attachment` table is where §1's cursor lives.

## Order

**Done: render narration in the console** — the smallest thing that made the console a channel
rather than a viewer, from rows that already existed. What remains:

1. ~~**Live updates** (§4)~~ — done.
2. **Stream the increment** (§4). Ahead of the merge by the operator's choice, so the surviving
   surface never reads worse than `/chat` does. Its own first step is the `authored` writer for the
   operator's prompt, without which the cursor does not cover the transcript.
3. **Merge the two pages** (§2) and move sending into the merged detail (§5, SPA half).
4. **Record lifecycle events** (§3) and render them in both channels. The `authored` arm has its
   first two writers — a lease taken over and a lease lapsing; `_SessionStatusAnnouncer`'s
   transitions are what remain.
5. **The reconcile loop** (§1) — the cursor on `chat_attachment`, and Matrix delivery moved onto
   it. Cleanup stage 7's schema half landed as migration `0064` and what the channel has already
   put in its copy as `0067`; what it still wants is the cursor and the readers moved onto it,
   which waits a release for `sessions.conversation_id` to be written by every replica. Everything
   above is possible without either.
6. **The Matrix relay** (§5, Matrix half) — one more thing the loop already does, once it exists.

Two items sit outside that spine. **The session link in the R7.2 notice** (§6) is small and can
land any time after §2 has settled the route — not before, since a posted link is permanent.
**Slash commands** (§6) depend on nothing here at all; they are ingress work, and abort is the
one worth having first.

## Open questions

- **Which command namespace survives the client?** Element consumes leading-slash verbs it
  recognizes and errors on ones it does not, so the choice is a compatibility question rather
  than a taste one (§6).
- **Can `@haku` set room state at all?** The room-level status in §6 rests on it, and the DM's
  power levels probably say no. One `kubectl`-free check in Element answers it.
