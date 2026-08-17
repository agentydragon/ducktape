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

- **A per-attachment cursor.** Cleanup stage 7's
  `chat_attachment(session_id, surface, address, attached_at, detached_at)` gains "delivered
  through". That is the entire durable state of a channel.
- **A per-channel projection**, because not every recorded event belongs on every channel — and
  "nothing to show" is a valid answer that still advances the cursor.

Two gotchas, both about the end that is not a database:

- **A room is append-only and federated, so an over-eager reconciler double-posts permanently.**
  The existing guard fits, and fits _better_ here than under a queue: `EventTag.transaction_id`
  derived from the transcript row is stable across processes and retries, so two reconcilers
  reaching the same conclusion still produce one event. Its limit is Synapse's 30–60 minute dedup
  window (<../docs/chat_runtime_facts.md>); past that the cursor is the only guard, which is the
  argument for advancing it in the same transaction that records the send rather than after it.
- **Convergence is not available at every edge.** Redacting a status message and clearing a typing
  indicator have no cheap "what does the room currently show" to compare against. Keep them out of
  the loop entirely — they are live-state rendering driven by the turn (§3), not cursor-driven
  delivery.

## 2. One sessions surface, not two pages

`/_console/chat` creates and drives one session over SSE; `/_console/conversations` lists every
session and renders a read-only transcript from one REST fetch. Same object, two routes, two nav
entries, two data paths, two view models over the same rows.

Merge them into one **sessions** surface: the list gains "new session", the detail gains the
composer, the abort button and close. `ClaudeChatPage` stops existing; what it knows how to do
becomes actions on a session detail.

**The live-update path is where the merge costs something.** `/chat` streams token-by-token over
SSE; the merged view updates by refetching on a coalesced notification (§4), which reads as
near-live but is not per-token. Take the regression knowingly:

- It is one live path for the whole console instead of a second one for a single page, and Matrix
  has no streaming either (matrix_chat_runtime.md § Non-goals), so it is not a capability the
  other channel has.
- Keep `/api/sessions/{id}/stream` until the merged view has proven a coalesced refetch is
  enough, then retire it. Retiring it is what makes the `asyncio.wait` abort dance in `_run_turn`
  removable — the simplification Phase 1 deferred.
- **The delta path itself does not go away**, and nothing here should be read as saying it does.
  `RolloutRecorder` records deltas with no exclusions because the fold needs a log with no hole
  (<../../plans/chat_runtime_projection.md> § stage 1), and `update_partial_frame` is what makes
  an interrupted turn's half-written answer survive (R5.5b). Only the SSE transport is in
  question.

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
`holding`, `room`) is Matrix-side today — what the console meant by an event _it sent to a room_.
Under this design `lifecycle` and `narration` become channel-neutral session events and the
Matrix tag derives from them; `status`, `holding` and `room` stay Matrix's own, because they
describe that channel's rendering. Do not promote the whole enum.

## 4. Live updates, over the socket the shell already holds — done

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

**Later, and deliberately not now: the backend streams the increment.** The operator wants the
server to push what changed rather than have the page re-read a whole conversation on every
invalidation (2026-08-16). Invalidate-then-refetch was the right first move — it is idempotent, so
a reconnect that missed events still lands correct, which is the property §1 turns on — but it
leaves the O(session)-per-update read <../../plans/chat_runtime_cleanup.md> § Anytime describes,
now paid per open tab as well as per delta. Two things have to exist before an increment is
sendable at all, and both are on the way: an ordered neutral event stream to name the increment in
(<../../plans/chat_runtime_projection.md> § stage 4), and a per-consumer position in it — which is
§1's cursor with a socket at the far end instead of a room. So this is a **consequence of the
neutral layer, not an optimization to schedule against it**, and until it exists the refetch is the
honest implementation. Recorded here rather than built.

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
R11.5), session → its tool calls, tool call → the session that made it. The last of those shares
its mechanism with console/TODO.md's per-tool-call deep link, which is unbuilt for the same
reason: nothing yet opens _the exact thing_ a URL names.

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
2. **Merge the two pages** (§2) and move sending into the merged detail (§5, SPA half).
3. **Record lifecycle events** (§3) and render them in both channels. The `authored` arm has its
   first two writers — a lease taken over and a lease lapsing; `_SessionStatusAnnouncer`'s
   transitions are what remain.
4. **The reconcile loop** (§1) — the cursor on `chat_attachment`, and Matrix delivery moved onto
   it. Wants cleanup stage 7's schema half; everything above is possible without it.
5. **The Matrix relay** (§5, Matrix half) — one more thing the loop already does, once it exists.

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
