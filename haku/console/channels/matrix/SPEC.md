# The Matrix channel — what it guarantees

Matrix is one channel onto the chat runtime: a transport for prose, plus the notices that make a
run legible from a phone. The console remains the session owner, the credential holder and the
approval authority; nothing here is a store of record. Delivery is reconciled per attachment — one
owner per bound room for its cursor, outbox, revisions and send budget; the redesign work still
open lives in <../../plans/conversation_layers.md>.

Out of scope by decision, so it is not re-litigated: end-to-end encryption, federation, approvals
over Matrix, and any write surface for the agent beyond its own replies.

## Ingress

- **Inbound events arrive by `/sync` from a bot account**, with the `next_batch` token persisted
  after each processed batch. The sync loop does no agent work inline: it persists and enqueues,
  then goes back to syncing.
- **Enqueue succeeds with no sandbox running and no runner connected.** Ingress must never wedge on
  the session's state.
- **Events authored by Haku's own identity are never treated as input.** The filter is on the sender
  MXID, which is also what makes a relayed operator message safe: it cannot loop back and be
  answered twice.
- **No inbound message is silently dropped.** An event that cannot be mapped, or that the session
  will not take, surfaces to the operator rather than vanishing — a rejected batch is said so in the
  room, naming what to wait for, and an unreadable event (an `m.image`, a voice memo, an msgtype
  invented after this release) is announced the same way. **Surfaced rather than refused**, because
  refusing does not converge: nothing about an already-sent screenshot ever changes, so the batch
  would be re-offered forever and one image would wedge ingress against every later message.

  Both are **recorded** as `conversation_event` rows written in the transaction that advances the
  watermark, so the room's line is a rendering of a fact the console kept rather than the only copy
  of it.

- **A re-delivered event is recognised rather than asked again.** Accepting a batch and advancing
  the stream position are two commits, so a crash between them hands the same events back. Ingress
  keys on the Matrix `event_id` and drops what a prompt in the record already carries. The key is
  written in the prompt's own transaction, which is what makes "already carried" mean the record
  holds it rather than that some process once saw it.

- **A message accepted by a session that then died is asked again**, on the session that replaces
  it. **Suppression is not acknowledgement:** skipping a re-delivered event and letting the position
  advance would trade a duplicate ask for a lost message, because the session holding the prompt can
  still die before answering it. So what is outstanding is worked out from the record rather than
  from the position — on every pass, which is also what a console that has just restarted does
  first. The room is told, since a question reappearing with no operator gesture behind it otherwise
  reads as Haku answering something twice.

- **No message is lost across console downtime**, however long the gap: messages that arrive while
  the console is down are processed once it returns, in order. If recovery cannot close a gap it
  says so loudly and never skips one silently.

  **Gotcha:** a persisted stream position is necessary but not sufficient. The homeserver answers a
  resumed sync with a **truncated** view of a long gap — it flags the truncation rather than
  erroring, so the missing span is visible only to a reader that checks. `test_homeserver_e2e.py`
  is the test that exists for this: a real Synapse, a room overfilled past `TIMELINE_LIMIT` with the
  sync loop stopped, every message back in order and once.

- **A first run has no missed range.** With no stored position there is nothing to recover, so the
  console takes a position rather than treating existing room history as a backlog to answer. A
  pending invite is still seen.

## Batching and admission

- **Pending wakeups coalesce into a single turn.** Three messages arriving together produce one
  turn, not three.
- **Batch order follows the homeserver's stream order** and is preserved in the rendered prompt.
- **A batch that arrives mid-turn is rejected, not held.** The room is told it was not delivered and
  the operator sends it again; nothing queues behind a running turn. Acceptance is the
  acknowledgement, so the watermark advances every pass. The cost is deliberate: mid-turn steering
  and a conversation-layer queue are the richer answers that would take it back, and neither is
  built.
- **What a prompt answered is recorded, not rendered.** The `PROMPT_ENQUEUED` event's body names the
  prompt's origin — a closed union of the SPA and a Matrix room, required with no default, so every
  stored prompt says which. The room's arm carries the room beside the events folded into the prompt,
  because one bot serves many rooms and a bare event id cannot tell a sibling room's copy from this
  room's. Both strings are opaque outside this channel: everything else compares them and never
  reads them.
- **Unsettled, and tracked in <../../TODO.md>:** the batch size cap and its overflow split, the
  ingress debounce window, and the age fence that makes a very old message context rather than work.
  Provenance in the rendered prompt is thinner than intended: `_as_prompt` renders one body per line,
  so sender, timestamp and thread root do not reach the agent.

## The room and the session behind it

- **A DM, not a room.** Each conversation is a direct chat between the operator and Haku. Nobody
  else can speak in it, which is why mention gating, per-room sender allowlists and multi-bot loop
  protection are absent here rather than overlooked.
- **One bot serves many rooms.** Every room the operator invites Haku into binds a conversation of
  its own, served beside the others: per attachment, its own cursor, reply outbox, editable-line
  revisions and send budget, so one room's backlog or refusals never reorder or starve another's.
  Nothing crosses rooms except the `/sync` stream and the credential.
- **The room is created by invitation, and Haku joins itself.** The operator starts a DM from any
  client; the harness sees the invite in `/sync`'s `rooms.invite` and joins — **only invites from
  the operator's own MXID**, the one mapped to an operator identity. An invite from anyone else is
  left pending and surfaced, never joined. Federation is off, so the possible senders are local
  accounts only, but "some local account can make Haku join a room" is not a property to concede by
  default.
- **An unbound room is adopted from traffic.** A room Haku is already joined to and is being spoken
  to in is one the operator put it in, since membership required an invite. Only a message from the
  operator triggers adoption, so the authorisation rule is unchanged. Without this, a room joined
  before a binding existed goes quiet permanently with no way to revive it from a Matrix client.
- **Nothing releases a binding.** Moving Haku to a different room is a database edit, not an
  operator gesture. The natural trigger is Haku being removed from the room, which `/sync` reports
  under `rooms.leave` — deferred because the session behind the binding is still running with a live
  sandbox, so the leave path has to dispose the session as part of unbinding or accept a leak.
- **A replacement session is re-awakened, not started blank**, and **the console's own transcript is
  the source, not the homeserver's copy of the room**. Matrix is one pluggable channel among
  several, and nothing may reach a channel except through our record; a second channel whose API
  cannot page history could not reproduce a memory read back out of the room. The first prompt of a
  replacement session carries the last N conversational messages plus how to read further back, with
  no summarisation step — a rotation mid-topic loses the thread's earlier reasoning, and the
  operator can say so and be answered from the room.

  Two things the room knows and our record does not, both accepted: history from before we were
  recording, and a redaction — the operator unsaying a message removes it from the room and not from
  the transcript.

## What the room shows while a turn runs

- **A turn in progress is visible without the agent doing anything.** The harness sets a typing
  notification when the turn starts — immediately, since "Haku is working on it" is worth nothing
  after the fact — refreshes it every ten seconds, and clears it on **every** terminal path
  including failure. The homeserver's own 30-second expiry is the backstop for the one path no code
  runs on, a console that dies mid-turn.
- **For slow turns one work line reports what is happening now**, created lazily after a latency
  threshold so short exchanges do not leave a status/answer pair behind. It is edited at most once
  every five seconds, **stays bounded however long the turn runs** — the running tools plus a tally
  of the calls already done, never a line per step — and is redacted on every terminal path.
- **Status is a coarse state, not a description of the work.** Where a tool is named, its identifier
  passes through verbatim: no per-tool copy, no mapping table to maintain as the tool surface grows.
  It is derived by the console from the neutral event stream it is already consuming — never by
  asking the model what it is doing, and never from one provider's own prose.
- **A session's pre-turn life is one edited line, and its ending stays in scrollback.** Sandbox
  provisioning, setup narration and adoption edit one `m.notice` per session instead of posting one
  each; the line is withdrawn once the first turn proves the session alive, and a lease expiry
  seals the ending — as that line's final edit while it still shows, or as a one-event notice when
  it does not. Rejections, unreadable input, aborts and failures stay notices of their own: those
  are facts an operator scrolls back for. System messages use `m.notice`, so clients render them
  distinctly and well-behaved bots do not react to them.
- **Every editable line has a durable identity: the conversation event that opened its span.** The
  tag every create, edit and seal carries names it, which is what lets a successor replica edit the
  line its predecessor posted, the room's own copy hold correspondence for the editable lines, and
  a takeover sweep redact a line whose span nothing open accounts for.
- **The current session ID is visible in the room**, at minimum on startup and on rotation, so the
  operator can quote it when debugging without asking the agent or opening the console. The agent is
  told its own session ID in its prompt too.

## Replies

- **The agent's reply is forwarded by the harness; the agent calls no tool to speak.** **Every**
  assistant message of the turn is forwarded as it finishes, not only the final one — a turn that
  says what it is about to do, works, and reports back is three messages, and forwarding only the
  last makes the room watch a long turn in silence. The turn's `result` frame repeats its last
  assistant text, so it is delivered only when nothing was said along the way.
- **Every turn speaks.** There is no silence token: a turn that finishes with no text says so, as a
  notice rather than a reply — the console reporting an outcome, not the agent talking.
- **A produced reply is never lost silently.** `matrix_outbox` holds it from the moment the room's
  subscriber reads the message complete, and the drain retries until the homeserver takes it. Each row is sent under its own
  stable transaction id, so a late redelivery inside Synapse's dedup window is refused rather than
  duplicated (<../../docs/chat_runtime_facts.md>).
- **A reply arrives formatted.** The event carries both forms — `body` stays the Markdown, which is
  the spec's fallback and what a plain-text client should show, and `format:
"org.matrix.custom.html"` plus `formatted_body` carries the rendering. Lifecycle notices stay
  plain.

  **The harness formats, and the agent is not asked to.** Being told to emit Matrix's HTML subset
  would make every reply a chance to emit a tag that is **silently** dropped, and would cost the tag
  list in prompt budget on every turn. What the agent is told is the smaller, stable thing: which
  affordances exist — `<details>`, spoiler and colour spans, `<u>`, `<sub>`/`<sup>`, a table
  `<caption>` — since Markdown has no syntax for them and they pass through to the room.

  **Conversion is against the spec's allowlist, applied to the output.** Everything outside it is
  unwrapped to its text here, where the fallback is deliberate, rather than at the far end where it
  is silent. Applying it to the output rather than trusting the input is what makes raw HTML the
  agent typed safe. Two cases a stock renderer gets wrong, both losing content rather than styling:
  **task lists** emit `<input type="checkbox">`, which is not allowlisted, so a checklist would
  arrive as bare bullets with its state gone — Haku writes checklists, so the state becomes `☐`/`☑`
  text; and **external images** are dropped, since `src` must be `mxc://`, so an image becomes its
  alt text.

## The room's own copy

- **Haku's own events are read back, and are still never input.** The same `/sync` ingress reads
  carries the console's own sends; ingress keeps dropping them by sender, and a mirror reader keeps
  what their tags say, durably and per attachment.
- **A sealed notice posts once, however the console dies.** A notice projected from a durable
  conversation event is not sent again when the room already shows an event tagged with that
  source, no matter how long the console was down — Synapse's transaction-cache deduplication only
  has to cover the window between a send and its echo becoming visible. In that window's one losing
  case, the echo itself dying with the console, the duplicate that lands is redacted once both
  copies have been observed, keeping the earliest.
- **A redaction is respected, not fought.** The operator unsaying Haku's copy removes it from the
  room; the console neither re-posts it nor treats the redacted copy as a duplicate to repair.

## Credentials and identity

- **No Matrix access token is present in the sandbox.** The console holds the single Matrix
  credential. That constrains the Matrix credential specifically; it is not a rule that the sandbox
  may hold none.
- **The console mints its own access token**, logging in with a provisioned password rather than
  being handed a token, so it can replace the token itself the moment Synapse stops accepting one —
  which Synapse does whenever the account's password is set again. It pins a `device_id` so repeated
  logins reuse one device and caches the token rather than logging in per request, because Synapse
  rate-limits `/login`.
- **The password Secret is a soft dependency.** The console starts, serves and stays up without it;
  the Matrix loop reports itself unconfigured rather than crash-looping. It is genuinely absent
  between first deploy and the reflector copying it, and a crash-loop there would take the approval
  queue down with it.
- **A Matrix sender maps to a console operator identity** via the Authentik OIDC subject. An
  unmapped sender gains no authority, message bodies are untrusted input, and **a Matrix message is
  never consent for a tool call**.
- **Approvals are unchanged by this channel.** A turn that hits an approval-gated tool gets whatever
  the console's synchronous wait returns — typically a `pending_approval` stub the agent can mention
  in its reply — and the operator resolves it in the console.

## The agent's own view

- **Reading only.** The agent's Matrix-facing surface is read: fetch by event ID, fetch around an
  event, paginate history. There is no send, edit, react, redact, join, invite, leave or room-state
  capability; speaking happens by auto-forward. Reads are served by console tools on the existing
  `/mcp` rather than by a Matrix credential in the sandbox, because **the room is not the only
  corpus and it is the smaller one** — what Haku _did_, tool calls and their results, is in the
  console's database and no Matrix credential of any shape reaches it.

  **The escape hatch, if three read tools prove too narrow** (threads, relations, reactions): the
  sandbox carries a placeholder token and the egress proxy substitutes the real value only for the
  homeserver host, as the Anthropic OAuth proxy already does. Still one Matrix credential, still
  none of it in the sandbox. The cost to settle first is that the secrets transform is
  **host**-scoped, so fencing off send, join and admin needs a path allowlist it may not have.

- **Reads cross rooms and past conversations; the fence is the reader's profile closure, not the
  room.** One decision function at the console (`conversation_read_access.py`, the profile DAG
  over each conversation's pinned profile) scopes the drilldown and semantic recall alike — not
  scoping smeared through the transport, which is a second reason to keep the tools plain HTTP
  entries rather than closures over a session. The tier generalization of that label remains
  planned (<../../../plans/information_trust_tiers.md>).
- **IDs are given, not guessed.** Nothing renders one into a prompt, so the only ones the agent holds
  are what the operator pasted; a permalink is accepted as input, since that is what a client
  produces on "copy link". A finding drawn from a room message cites the message, in a form the
  operator can click.
- **The room read tools are unbuilt**, so the system prompt no longer tells the agent an event ID is
  citable — the harness could resolve only one it had already shown, which is a promise it could not
  keep. What a prompt was folded from rides on the prompt's own event instead, which is what a read
  tool will resolve a citation through once one exists.

## Deployment

- Rooms are unencrypted and federation stays off.
- Haku's Matrix account and its credential are GitOps-managed, never hand-minted outside incident
  diagnostics: `cluster/provisioners/matrix_user_provisioner` registers `@haku` from a SOPS
  password, reflected into `haku-console`.
- All console-to-homeserver traffic is cluster-internal and outbound, so it needs neither an
  egress-proxy exception nor any inbound NetworkPolicy: nothing in `matrix` connects to the console.
- Matrix lives in OVH (`zone: hil-ovh`), with the media store on `seaweedfs-ovh` and the database on
  the OVH-HA CNPG profile. The SeaweedFS CSI node plugin runs only on the OVH nodes, so this is a
  placement constraint rather than a preference.
