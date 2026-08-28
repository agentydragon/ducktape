# The chat runtime's three layers

The console's chat runtime has three layers — session, conversation, channel — and the conversation
is the only thing the other two talk to:

> A **channel** listens to and sends to the **conversation**, never to a session.
>
> A **session** listens to and sends to the **conversation**, never to a channel.

The tables that realise this are the chat half of <../database_schema.py>; the evidence that their
vocabulary belongs to no one backend, and the invariants spanning them, are in
<conversation_schema.md>.

## What each layer owns

**Session** — one runner incarnation. It owns the wire log (`session_frames`), the turns cut out of
it, its sandbox claim, and the lease that makes one replica its single writer. Provider-specific
shape stops here: a CLI frame's `type`, its content blocks and its `result` envelope are readable at
this layer and nowhere above it. A session ends when its runner does, and a conversation outlives
however many of them it takes.

**Conversation** — the durable, provider-neutral record of one thread, pinned to one
`runtime_kind`: an ordered stream of neutral events, addressed by position, plus the transcript
rows those events point at. The pin says which implementation owns prompt/context/projection/replay
semantics across replacement sessions; it does not put that implementation's wire vocabulary into
the record. A conversation is what every reader reads and the only thing a channel is offered. It
has no end.

**Channel** — how one messaging service holds and interacts with a copy of a conversation. It owns
its address, its credential, its rate budget, its rendering vocabulary, its position in the
conversation's stream, and whatever delivery state its transport needs. A browser tab is a channel
too; it differs only in holding no copy that outlives it, so its position is an argument to its next
read rather than a durable cursor.

## The edges

Two exist:

- **session ↔ conversation.** The session folds its frames into the record; the conversation
  admits prompts to the session and hands a replacement session the thread's tail.
- **channel ↔ conversation.** The channel subscribes to the record from its own position and
  renders what arrives; it offers the conversation inbound input, which the conversation may refuse.

Two do not:

- **session → channel.** Nothing at the session layer names a channel, holds one, is handed one, or
  is selected by one. A turn writes the record and stops.
- **channel → session.** Nothing at the channel layer names a session, reads a session-keyed row,
  keys its own durable state by a session, or creates, replaces or tends one.

Three consequences, since they are the ones that get argued:

- **A session id is not a channel-visible identifier.** A channel that means "the same thread" says
  `conversation_id`. This binds what a channel stores outside Postgres hardest of all: a room event
  is permanent and federated, so an id in its tag outlives every session it could name.
- **A fact a channel shows exists conversation-side first.** Recording it is what makes it
  showable; handing it to the channel from whatever noticed it is not. A fact whose only copy is a
  stack frame, a per-process latch or a queued closure is one a second channel cannot show and a
  SIGKILL erases.
- **The layer of a fact is not the layer of its cause.** "This session's lease lapsed" is caused by
  a session and is a conversation fact, because it is something the operator is told in a room. Who
  may read it decides the layer; the session it names is a field on the row, not the row's key.

## How a fact reaches the record

Two routes, and the category a row lands in says which one it came by — never what the fact is
about:

- **Folded out of frames.** The session's single writer projects recorded frames into neutral events
  inside the transaction that advances its projection cursor, so an event exists exactly when the
  cursor says its frames were projected. Such a row carries the inclusive frame range it was read
  from, which is what lets an operator appeal it to the wire and what lets a session be
  re-projected.
- **Authored, because the console is the only witness.** A prompt accepted before any runner exists,
  a lease changing hands, a sandbox being provisioned for the thread, an operator stopping a turn.
  Such a row names no frames, and may name no session either.

Re-projection therefore rebuilds only the first category and must preserve the second: a rebuild
that re-derived everything would silently delete every fact no frame carries.

## Placing something new

- **A module** goes to the directory of the one axis it varies on: the channel-neutral,
  harness-neutral runtime (`../x/*.py`), one channel (`../channels/<name>/`), and one
  CLI harness each in a directory named for the product whose binary it launches (`../x/claude_code/`,
  `../x/codex_app_server/` — the harness and the model behind it are different axes). The test is what
  the module would take with it if the other axis were replaced: a second channel must reuse
  everything at the runtime level unchanged, and a module that cannot compile without `matrix-nio`
  is the channel's. Imports do not decide the harder cases — what the module's output is _for_
  does. `channels/matrix/spans.py` imports no `matrix-nio` and reads only the neutral stream, yet
  belongs to the channel, because what it folds the stream into is Matrix's own rendering and a
  second channel writes its own fold; `x/sandbox_claims.py` mints `claude-`-prefixed claim names
  but is Kubernetes provisioning any harness uses, so it is runtime. A vocabulary genuinely owned
  by two axes is two modules: the CLI's own top-level `type` values are `x/claude_code/frames.py`,
  and the bridge envelope's `kind` with the row the console authors under it is `x/setup_output.py`.
- **A table** goes to the layer that outlives what it holds. State only one messaging service can
  interpret — retry budgets, transport ids, an addressable copy's revision — is that channel's own,
  lives below the channel boundary and is named after the channel. State that must survive a runner
  being replaced is not the session's. What is left is the conversation's.
- **A port** is defined beside its caller and names no layer below it. The conversation offers one
  port outward — subscribe from a position, and offer input — and every channel is a consumer of
  it; a second channel is implementing that port and a cursor, and nothing else in the console
  changes.
- **A subscription** is that port's read half, and there is exactly one protocol for it: the
  positional read of the record, plus the conversation-keyed wake that says to look now — the wake
  carries an address and never content, so the record stays the authority. Whatever transports a
  consumer — a room, a follow socket, the console socket a tab already holds — what it subscribes
  to is that pair. A new way to learn that a conversation moved is a second protocol, and the
  answer is to be a consumer of the one that exists; the runtime side is symmetric, learning of
  conversation demand by the same wake rather than by a path of its own. The protocol governs the
  layer boundary, not the inside of a component: a signal that never crosses one — channel code
  waking channel code about the channel's own delivery state — rides that component's own wiring,
  below its boundary, and does not join this wire.
- **An event kind** joins the record if any reader outside the session that produced it may need
  it, and takes its category from the route it arrived by, not from its subject. A kind that only
  a running turn reads is a session detail and needs no row; a kind a room or a tab renders is a
  conversation fact, however session-shaped it sounds.

Four questions check a change against all of this:

- Does anything outside the channel layer name a channel — an import, a type, a parameter, a
  column, an address?
- Does anything inside a channel name a session?
- Does a fact this change makes visible have a row of its own, or only a caller that happens to
  know it?
- Does a durable artifact this change puts outside Postgres carry an identifier that replacing a
  session invalidates?
