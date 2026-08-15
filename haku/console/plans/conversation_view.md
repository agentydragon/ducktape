# The conversation view as a live surface

**Status: proposal.** The read-only inventory and detail view shipped in #4063. This is what
turns it into something worth leaving open: it updates itself, and eventually the operator can
speak into a conversation from it. Stage A is the near-term want; stages B and C are
deliberately lower priority and are written down so they are not redesigned from scratch.

## What exists

`frontend/x/conversations_page.tsx` fetches once on mount and never again — the list from
`GET /api/conversations`, the detail from `GET /api/conversations/{session_id}`, both
operator-scoped in `x/claude_chat.py`. There is no composer and no refresh; watching a Matrix
turn arrive means reloading the page.

Two live mechanisms already exist in this console, and which one this builds on is the first
decision:

| Mechanism                                | Shape                                                                                                                                                            |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/api/claude/sessions/{id}/stream` (SSE) | One stream per session, opened by the SPA chat page. Wakes on `ChatEventKind.UPDATE`, re-reads the session and re-serializes the **whole transcript** each time. |
| `/api/events/ws` (`console_events.py`)   | One websocket per operator, held by the shell for the life of the tab. Typed `ConsoleEvent` union, fanned across replicas over its own Postgres channel.         |

## Stage A — live updates over the console event socket

**Build on the websocket, not on a second SSE endpoint.** The shell already holds exactly one
`/api/events/ws` per tab, and `frontend/console_events.ts`'s `useConsoleEvents` already provides
the whole client half: reconnect with backoff, a `LiveStatus` for the rail, and a callback that
fires on mount, on every event, **and again on reconnect** — which is precisely the
"read once, then read again whenever something might have changed" contract this page wants. A
per-session SSE stream would be a second socket, a second auth path, and a second reconnect
story for a page that is one of several in the same tab.

**Send a notification, not a payload.** A new `ConversationChangedEvent {session_id}` in the
`ConsoleEvent` union; the page refetches the endpoint it already has. This is the shape
`ToolCallsChangedEvent` already established, and it is the one that stays correct when a
reconnect means the client missed events entirely — a refetch is idempotent where a delta
stream has to be replayed.

Four things to get right, in increasing order of how easy they are to miss:

- **No second Postgres channel, and no second `NOTIFY`.** `LISTEN` is broadcast, so every
  replica's `ChatNotifications` already receives every `claude_chat` event. The fan-out is local:
  a replica turns the chat events it is already receiving into sends on the console-event sockets
  **it** holds. Routing this through `ConsoleEventHub.broadcast` (which relays over its own
  channel) would notify twice for one update and deliver to every replica twice.
- **Coalesce, and treat that as load-bearing.** `ChatEventKind.UPDATE` fires **per stream delta**
  — hundreds per turn. One websocket frame per delta, each triggering a full-transcript refetch,
  is the same O(session)-per-token-batch cost `chat_runtime_cleanup.md` § Anytime flags on the SSE
  path, except now paid by every open tab. Debounce per `session_id` in the fan-out (a few hundred
  ms), so a streaming turn produces a steady trickle rather than a frame per token.
- **Operator scoping needs a lookup, and it must not be per event.** `ChatEvent` is
  `{kind, session_id}` with no operator on it, while the hub delivers per `operator_id`. Resolve
  session → operator once per coalesce window rather than per event, and cache it — a session's
  owner does not change. Do **not** reach for "just add `operator_id` to `ChatEvent`" without
  pricing it: the payload is a wire contract across a `maxUnavailable: 0` roll, so widening it is
  expand/contract over two releases (<../x/README.md> § the wake channel).
- **The list page wants the same event.** An inventory showing `message_count` and `updated_at`
  is stale for the same reason the detail view is. Same event, same refetch; the page decides
  whether the `session_id` is one it is displaying.

**Done when** an open detail view shows a Matrix turn arriving without a reload, and there is no
polling timer anywhere in the page.

## Stage B — sending into an SPA conversation

Nearly free: `POST /api/claude/sessions/{id}/messages` already exists and already does the durable
thing (`enqueue_prompt` writes the transcript row and the `claude_chat_prompts` row in one
transaction). What it costs is one honest limitation and one route decision:

- **`enqueue_prompt` refuses while a turn is open or a prompt is queued**, with a 409. That is
  deliberate — admission asks the turn, and accepting mid-turn would be the fold-into-turn feature
  arriving by accident. So the composer is **disabled during a turn** and says why; it is not a
  queue. Mid-turn steering (<../../plans/chat_runtime_cleanup.md> § Later) is what would relax
  this, and it is measured to work but unbuilt.
- **Do not grow a second send route.** The view reads `/api/conversations/...` and would send to
  `/api/claude/sessions/...`. Either the page calls the existing route, or sending moves to
  `POST /api/conversations/{session_id}/messages` and the SPA chat page follows. Prefer the
  latter **once stage C exists**, because stage C's send is not the SPA's send and the two want
  one entry point that dispatches on the conversation's surface — which is the `ChatFrontend` port
  <../../plans/chat_runtime_cleanup.md> § The frontend seam already argues for.

## Stage C — sending into a Matrix conversation

The interesting half, and the one explicitly lower priority.

**The problem.** The console holds one Matrix credential, `@haku`'s (R5.1). It cannot post as the
operator's MXID, so a console-originated message either does not appear in the room at all, or
appears under Haku's account.

**Not posting it is ruled out**, because three readers would then see half a conversation:

1. The operator's own Element — the surface the room exists for.
2. `recent_messages`, which is what re-awakens a replacement session (R3.3a). A rotation would
   restore Haku's answers with the questions missing.
3. Any room read tool (R11.3), when it lands.

**So: a relay message.** `@haku` posts the operator's text under a new `RoomEventKind` — `relay`
— tagged in `works.allegedly.haku` like every other console-authored event, and rendered so the
room states its true provenance: written by the operator, delivered by Haku's account because
theirs is not the console's to speak with.

What that costs, in increasing order of subtlety:

- **`_is_conversational` must include the new kind.** It reads
  `tag is None or kind is REPLY` today, encoding "everything the console says _about_ the
  conversation is not the conversation". A relayed operator message is the exception — it **is**
  the conversation. Get this wrong and every rotation re-awakens a session with every
  console-sent prompt missing from its context, silently.
- **Ingress needs no change at all.** R1.5 excludes Haku's own sender from input, so a relay
  cannot loop back and be answered twice. This is the one place where posting under `@haku` is an
  advantage rather than a compromise.
- **The room and the transcript must not diverge.** Enqueue and post are two writes and either
  order has a bad failure. **Enqueue first**: a room missing a message is recoverable and visible,
  where a turn answering a message the room never showed is neither. Retry the post with
  `EventTag.transaction_id` derived from the transcript row — already the rule for replies — so a
  retry cannot double-post inside Synapse's dedup window
  (<../docs/chat_runtime_facts.md>). **This is a second argument for the room outbox**
  (<../../plans/chat_runtime_projection.md> § stage 5): with it, "enqueue and post" is one
  transaction and the retry is simply what an outbox does. Stage C is buildable without it and
  materially better with it.
- **Provenance in the prompt, without inventing an event id.** R2.4 gives each batched message a
  sender, timestamp, `event_id` and thread root. A console-originated message has a stronger
  identity than any of them — an authenticated operator session, not the MXID mapping of R9.3 —
  and **no event id yet**, because the relay may not have posted when the prompt renders. Say it
  came from the console and carry the transcript message id. R11.4's "IDs are given, not guessed"
  cuts both ways: an absent id is honest, a fabricated one is not, and the turn must not block on
  the post to obtain one.
- **The same 409 as stage B, with less margin.** Matrix ingress absorbs a refusal by not advancing
  the sync watermark and letting the homeserver redeliver. A console send has no homeserver behind
  it, so a refusal has to reach the operator as a refusal — never a swallowed message.
- **Plain text, for now.** Replies carry `formatted_body` (R11.7) because the agent writes
  Markdown. The operator writes into a textarea; sending the body plain is honest and rendering it
  is a later choice, not a prerequisite.

**Rejected, and priced already:** giving the console the operator's own Matrix credential, and an
appservice with a puppet MXID for the operator. The first breaks R5.1's single-holder property for
a send button; the second reverses R1.1's whole design note for one. Neither is worth it.

## Order, and what each stage waits on

- **Stage A depends on nothing** in either chat-runtime plan. The coalescing is what keeps it off
  the O(session) refetch path, so it does not wait on that cleanup.
- **Stage B depends on nothing**, and is small enough to ride along with A.
- **Stage C wants two unscheduled things** and is blocked by neither: the room outbox makes its
  enqueue/post pair atomic, and mid-turn steering removes the disabled-composer window. Doing it
  first means accepting both rough edges knowingly.
