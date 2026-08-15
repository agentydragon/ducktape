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
| Live updates          | native                         | **no** — one fetch on mount    |
| Send a prompt         | yes                            | **only on `/chat`**            |
| Start a session       | no — the supervisor owns it    | yes, on `/chat`                |
| Abort a turn          | **no**                         | yes, on `/chat`                |
| Close a session       | no                             | yes, on `/chat`                |
| A turn is in progress | typing indicator + status line | **no**                         |
| Lifecycle transitions | `m.notice`, unrecorded         | **no**                         |
| Bootstrap narration   | `m.notice`, one line each      | **no** — though the rows exist |
| Formatted replies     | yes (R11.7)                    | yes (Markdown renderer)        |

**Parity is not symmetry.** Some gaps are load-bearing and stay: Matrix does not start sessions
because the supervisor owns provisioning (R3.1, R3.2), and the console does not bind or unbind
rooms (R3.6, R3.6c). The goal is that no channel is missing something for **no reason** — which
is what most of the bold cells above are.

## 1. One sessions surface, not two pages

`/_console/chat` creates and drives one session over SSE; `/_console/conversations` lists every
session and renders a read-only transcript from one REST fetch. Same object, two routes, two nav
entries, two data paths, two view models over the same rows.

Merge them into one **sessions** surface: the list gains "new session", the detail gains the
composer, the abort button and close. `ClaudeChatPage` stops existing; what it knows how to do
becomes actions on a session detail.

**The live-update path is where the merge costs something.** `/chat` streams token-by-token over
SSE; the merged view updates by refetching on a coalesced notification (§3), which reads as
near-live but is not per-token. Take the regression knowingly:

- It is one live path for the whole console instead of a second one for a single page, and Matrix
  has no streaming either (matrix_chat_runtime.md § Non-goals), so it is not a capability the
  other channel has.
- Keep `/api/claude/sessions/{id}/stream` until the merged view has proven a coalesced refetch is
  enough, then retire it. Retiring it is what makes the `asyncio.wait` abort dance in `_run_turn`
  removable — the simplification Phase 1 deferred.
- **The delta path itself does not go away**, and nothing here should be read as saying it does.
  `RolloutRecorder` records deltas with no exclusions because the fold needs a log with no hole
  (<../../plans/chat_runtime_projection.md> § stage 1), and `update_partial_frame` is what makes
  an interrupted turn's half-written answer survive (R5.5b). Only the SSE transport is in
  question.

## 2. Session-level events belong in the database, not only in the room

Four things get posted to the room today. They are not one kind of thing, and the difference
decides which become rows:

| What                    | Written by                                   | Recorded today                    |
| ----------------------- | -------------------------------------------- | --------------------------------- |
| Lifecycle transitions   | `_SessionStatusAnnouncer` (matrix_session)   | **nowhere**                       |
| Bootstrap narration     | `_progress_reporter` → `RoomSurface.report`  | **already** — `setup_output` rows |
| The in-turn status line | `_TurnStatus` → `show_status`/`clear_status` | no, and deliberately              |
| The typing indicator    | `_TurnStatus` → `set_typing`                 | no, and deliberately              |

**Record what happened; derive what is being shown.** The status line and the typing indicator
are Matrix _renderings of live state_ — R6.3 already says status is a coarse state the console
derives from the frame stream it is consuming anyway. The console channel should derive the same
state from the same source and render it its own way (an activity line, a spinner), not replay
Matrix's edited-message trick. Recording the status text would be recording a rendering, and it
is the one mistake this section exists to prevent.

**Lifecycle follows the precedent narration already set.** `_setup_output_frame`'s docstring
makes the general argument: a console-authored session event lives in the frame log rather than a
table of its own, because the question a reader asks is "what happened in this session, in
order" — and for a session that died before the CLI produced anything, the answer is entirely
there. Lifecycle is the same shape and is the clearest case for it: a session that never got past
`provisioning` has nothing else to show. So lifecycle events become frame-log rows under their
own bridge-side `kind`, not a new table, and ordering against everything else is free.

Two things fall out, both good:

- **The console channel can render bootstrap narration today**, from rows that already exist. It
  is the cheapest single item in this plan.
- **Announcement dedup stops being per-process.** `_SessionStatusAnnouncer._last_announced` is a
  local, so a leader handover re-announces the current status — legible in a room, but it also
  means there is no durable answer to "when did this session become `failed`". A row _is_ the
  record of having announced it.

**Vocabulary consequence.** `RoomEventKind` (`reply`, `status`, `narration`, `lifecycle`,
`holding`, `room`) is Matrix-side today — what the console meant by an event _it sent to a room_.
Under this design `lifecycle` and `narration` become channel-neutral session events and the
Matrix tag derives from them; `status`, `holding` and `room` stay Matrix's own, because they
describe that channel's rendering. Do not promote the whole enum.

## 3. Live updates, over the socket the shell already holds

**Build on `/api/events/ws`, not a second SSE endpoint.** The shell holds exactly one per tab, and
`frontend/console_events.ts`'s `useConsoleEvents` already provides the whole client half:
reconnect with backoff, a `LiveStatus` for the rail, and a callback that fires on mount, on every
event, **and again on reconnect** — precisely the "read once, then read again whenever something
might have changed" contract this wants.

**Send a notification, not a payload.** A `SessionChangedEvent {session_id}` in the `ConsoleEvent`
union; the page refetches. This is the shape `ToolCallsChangedEvent` already established, and it
is the one that stays correct when a reconnect means events were missed entirely — a refetch is
idempotent where a delta stream must be replayed.

Four things to get right, in increasing order of how easy they are to miss:

- **No second Postgres channel, and no second `NOTIFY`.** `LISTEN` is broadcast, so every
  replica's `ChatNotifications` already receives every `claude_chat` event. The fan-out is local:
  a replica turns the chat events it already receives into sends on the console-event sockets
  **it** holds. Routing this through `ConsoleEventHub.broadcast` (which relays over its own
  channel) would notify twice for one update and deliver to every replica twice.
- **Coalesce, and treat that as load-bearing.** `ChatEventKind.UPDATE` fires **per stream delta**
  — hundreds per turn. One frame per delta, each triggering a full-transcript refetch, is the
  O(session)-per-token-batch cost <../../plans/chat_runtime_cleanup.md> § Anytime already flags on
  the SSE path, now paid once per open tab. Debounce per `session_id` in the fan-out.
- **Operator scoping needs a lookup, and it must not be per event.** `ChatEvent` is
  `{kind, session_id}` with no operator on it, while the hub delivers per `operator_id`. Resolve
  session → operator once per coalesce window and cache it — a session's owner does not change.
  Do **not** reach for "just add `operator_id` to `ChatEvent`" without pricing it: the payload is
  a wire contract across a `maxUnavailable: 0` roll, so widening it is expand/contract over two
  releases (<../x/README.md> § the wake channel).
- **The list wants the same event.** An inventory showing `message_count` and `updated_at` is
  stale for the same reason a detail view is.

**Done when** an open session shows a Matrix turn arriving without a reload, and there is no
polling timer anywhere in the page.

## 4. Sending from the console

### Into an SPA session

Nearly free: `POST /api/claude/sessions/{id}/messages` exists and already does the durable thing
(`enqueue_prompt` writes the transcript row and the `claude_chat_prompts` row in one transaction).
One honest limitation and one route decision:

- **`enqueue_prompt` refuses while a turn is open or a prompt is queued**, with a 409. That is
  deliberate — admission asks the turn, and accepting mid-turn would be the fold-into-turn feature
  arriving by accident. The composer is **disabled during a turn** and says why; it is not a
  queue. Mid-turn steering (<../../plans/chat_runtime_cleanup.md> § Later) is what relaxes this.
- **One send route, dispatching on the session's channel** — not the SPA's route for SPA sessions
  and a second one for Matrix. That entry point is the `ChatFrontend` port §5 corrects.

### Into a Matrix session — lower priority

**The problem.** The console holds one Matrix credential, `@haku`'s (R5.1). It cannot post as the
operator's MXID, so a console-originated message either does not appear in the room at all, or
appears under Haku's account.

**Not posting it is ruled out**, because three readers would then see half a conversation: the
operator's own Element; `recent_messages`, which re-awakens a replacement session (R3.3a); and any
room read tool (R11.3), when it lands.

**So: a relay message.** `@haku` posts the operator's text under a new `RoomEventKind` — `relay` —
tagged in `works.allegedly.haku` like every other console-authored event, and rendered so the room
states its true provenance: written by the operator, delivered by Haku's account because theirs is
not the console's to speak with.

What that costs, in increasing order of subtlety:

- **`_is_conversational` must include the new kind.** It reads `tag is None or kind is REPLY`
  today, encoding "everything the console says _about_ the conversation is not the conversation".
  A relayed operator message is the exception — it **is** the conversation. Get this wrong and
  every rotation re-awakens a session with every console-sent prompt missing, silently.
- **Ingress needs no change at all.** R1.5 excludes Haku's own sender from input, so a relay
  cannot loop back and be answered twice. This is the one place where posting under `@haku` is an
  advantage rather than a compromise.
- **The room and the transcript must not diverge.** Enqueue and post are two writes and either
  order has a bad failure. **Enqueue first**: a room missing a message is recoverable and visible,
  where a turn answering a message the room never showed is neither. Retry the post with
  `EventTag.transaction_id` derived from the transcript row — already the rule for replies — so a
  retry cannot double-post inside Synapse's dedup window (<../docs/chat_runtime_facts.md>).
- **Provenance in the prompt, without inventing an event id.** R2.4 gives each batched message a
  sender, timestamp, `event_id` and thread root. A console-originated one has a _stronger_
  identity than any of them — an authenticated operator session, not the MXID mapping of R9.3 —
  and **no event id yet**, because the relay may not have posted when the prompt renders. Say it
  came from the console and carry the transcript message id. R11.4's "IDs are given, not guessed"
  cuts both ways: an absent id is honest, a fabricated one is not, and the turn must not block on
  the post to obtain one.
- **The same 409, with less margin.** Matrix ingress absorbs a refusal by not advancing the sync
  watermark and letting the homeserver redeliver. A console send has no homeserver behind it, so
  a refusal must reach the operator rather than be swallowed.
- **Plain text, for now.** Replies carry `formatted_body` (R11.7) because the agent writes
  Markdown; the operator writes into a textarea, so a plain body is honest and rendering is a
  later choice.

**Rejected, and priced already:** giving the console the operator's Matrix credential, and an
appservice with a puppet MXID. The first breaks R5.1's single-holder property for a send button;
the second reverses R1.1's whole design note for one.

## 5. What this corrects in the existing plans

Two places state that the SPA needs nothing from the room-facing port. That premise was true of a
surface whose client only read message rows; it is not true of a channel:

- **`RoomSurface`'s docstring** — "The SPA needs none of this — its client reads the message rows
  over SSE, so a finished turn is delivered by being written down." Under §2 the console channel
  wants lifecycle and narration, and under §3 it wants them pushed.
- **<../../plans/chat_runtime_cleanup.md> § The frontend seam** — "a null implementation for the
  SPA, which needs none of it". The `ChatFrontend` port with no address parameter is exactly
  right and survives unchanged; **the null implementation does not.** Both channels implement it.

One convergence rather than a conflict, worth naming so it is not rediscovered: that stage's
`chat_attachment(session_id, surface, address, attached_at, detached_at)` is precisely the
"which channels does this session have" table this design needs, and
<../../plans/chat_runtime_projection.md> § stage 5's room outbox generalizes to "record the event
once, deliver it to each attached channel" — which is §2 and §3 sharing one mechanism.

## Order

1. **Render narration in the console** — the rows already exist; smallest thing that makes the
   console a channel rather than a viewer.
2. **Live updates** (§3). Depends on nothing.
3. **Merge the two pages** (§1) and move sending into the merged detail (§4, SPA half).
4. **Record lifecycle events** (§2) and render them in both channels.
5. **The Matrix relay** (§4, Matrix half). Wants the room outbox and mid-turn steering, is blocked
   by neither, and doing it first means accepting both rough edges knowingly.

## Open question

**Should Matrix be able to abort a turn?** It is the one capability the console has and Matrix
plainly lacks with no stated reason. `_run_turn` already implements interrupt for the console's
abort button, and an operator watching a runaway turn from a phone currently has no way to stop
it. It would need an operator-side gesture in the room — a control message, or a reaction — which
is harness ingress, not an agent tool, so R5.4's read-only surface is untouched. Not decided here
because it is the first thing that would make a room message mean something other than "talk to
Haku", and that deserves its own argument.
