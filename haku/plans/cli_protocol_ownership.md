# Owning the CLI protocol

**Status: built** (decided 2026-08-12). The console drives Claude Code's newline-delimited JSON
protocol itself: <../cli_protocol/frames.py> types the control channel, `ClaudeCli`
(<../runtime/x/claude_bridge/cli_client.py>) reads both channels and owns `initialize` and
`interrupt`, and `options.py` builds the launch argv. No Python imports the Agent SDK. Why each of
those is ours rather than the SDK's is written where it is now maintained — those modules'
docstrings — and the wire itself is <../cli_protocol/protocol.md>.

The conversation channel stays deliberately unmodelled: the console's record of a session is the
wire, and a frame gets a model when the code that acts on it exists.

What is left here is what that decision did not finish — where the CLI binary comes from, the
capabilities owning the handshake put within reach, and the session re-adoption design this was a
prerequisite for.

## The CLI should come from npm, not out of a Python wheel — [later]

The Agent SDK wheel is still a **build** dependency, for one reason: it bundles the `claude` binary
the runner image needs. That thread should be pulled too. Anthropic publishes the CLI as
`@anthropic-ai/claude-code` (481 versions, per-platform binaries such as
`@anthropic-ai/claude-code-linux-x64`), so sourcing it from a Python wheel is not necessity but
habit, and a worse habit than it looks: `extract_claude.py` reaches into another package's
`_bundled/` for a file it happens to ship, and the CLI version is then pinned only as a side effect
of the SDK's. npm is the real distribution channel, this repo already manages npm through
`@aspect_rules_js` and pnpm, and pinning the CLI directly is what the version-pinning discipline in
<../cli_protocol/README.md> actually asks for. Cost is a `package.json` entry, a
`js_binary`-or-`filegroup` in place of the `claude_executable` genrule, and deleting
`extract_claude.py`.

## What owning the handshake buys — [later]

The protocol reference is <../cli_protocol/README.md>; field shapes, measured behaviour and the
probes that establish them live there and are not repeated here. This is only the judgment about
which of it Haku should take up. `initialize` is sent bare today, so all of it is available and none
of it is in use.

Worth taking up, in rough order of value:

- **`sdkMcpServers`** — the console hosts an MCP server itself, over the control channel, and
  the CLI speaks JSON-RPC to it. No second process, no port, no credential on the wire, and the
  tool implementation stays where the data already is. <matrix_chat_runtime.md> R5.2a passed on
  this when it bought structural session scoping we then decided against (R5.3a); as a way to
  give Haku console-side tools it stands on its own. This is the strongest candidate on the list
  for the transcript-reading API.
- **`jsonSchema`** — a bare JSON Schema the answer must satisfy, returned parsed on the `result`
  frame. Anywhere the console today parses Haku's prose, this replaces it with a structure.
- **`forwardSubagentText`** — a subagent's prose reaches the client only with this set; by
  default the client sees its tool calls and nothing it said. Relevant to R6's status line, next
  to the `system/task_*` frames that line already reads, and it is a volume decision as much as a
  capability one: a room does not want every subagent's narration.
- **`skills`** — an allowlist for what loads into the system prompt. A prompt-budget lever for a
  long-running session, and Haku's skill set is not small.
- **`hooks`** — they work, and a `PreToolUse` deny is honoured before the permission check ever
  runs, so this is a real policy seam. It is also inbound control traffic, which the re-adoption
  section below warns about: the hazard is an unanswered request whose **replay has side
  effects**, which a permission hook has and a read-only one does not. Read that first.

Two to know about without acting on:

- **`supportedDialogKinds`** fails closed, so we are already taking the degraded path silently —
  for `refusal_fallback_prompt`, the classic refusal error. Whether that is wrong depends on
  whether a Matrix room can host a blocking dialog at all, which is a surface question.
- **`toolAliases`, `planModeInstructions`, `excludeDynamicSections`, `title`,
  `agentProgressSummaries`** are accepted and unmeasured or inert here. `initialize` validates
  almost nothing, so any of them can be set in the belief it did something; check for an effect
  rather than for an error.

## Session re-adoption across a console roll

**Status: built (design B below).** A console replica that dies no longer takes its Claude Code
session with it: the runner keeps the CLI alive across the dropped socket and redials, and an
adopting console picks the conversation up mid-flight. The lease
(<../console/x/claude_chat.py>, `expire_stale_leases`) is what makes that safe — a dropped session is
**observable** (another replica notices the holder stopped renewing) and, because an expired lease
now means unowned rather than dead, **adoptable**: the returning runner takes it over and the sweep
fails the session only if none does. The design alternatives and the reasoning that chose B are kept
below.

### Why the sandbox is not the problem

The sandbox already outlives the console. It is a separate SandboxClaim with its own
`shutdownTime` driven by `session_ttl_seconds`, on its own pod, and the runner dials **out** to
the console — so a reconnect lands on whichever replica the Service picks, with no addressing
problem to solve.

What dies is the CLI process. `bridge_websocket_to_claude`
(<../runtime/x/claude_bridge/runner.py>) terminates it in its `finally` when the socket
closes. That single line is the difference between the two designs below.

### Two designs

**A — resume in the surviving sandbox.** Let the CLI die with the console; a new replica starts
a fresh CLI in the same pod with `--resume`. `CLAUDE_CONFIG_DIR=/claude-config` is already set
in the runner image, so the session state is on disk there. Cheap: no runner protocol change.
Loses the in-flight turn.

Worth noting even if B is what gets built: resume is strictly better than the re-awakening in
<matrix_chat_runtime.md> (R3.3a), which reconstructs context from the last N room messages.
Resume restores the actual context rather than an approximation of it, and needs no summary.

**B — keep the process alive across the gap.** The runner stops killing Claude on disconnect,
buffers, and redials; an adopting console picks the conversation up mid-flight. Preserves the
in-flight turn. This is what was built.

What B has to survive is the wire in <../cli_protocol/protocol.md>: two channels multiplexed on one
stream, the conversation half append-only — which is what makes replay tractable — and the control
half request/response correlated by `request_id`, and **bidirectional**, since both ends originate
requests.

### Gotcha: inbound control traffic is what normally makes this hard, and we have none

A control request is correlated by an in-memory `request_id` → future map. If the CLI has an
outstanding request to the console when that replica dies, nobody answers it, the CLI blocks
forever, and an adopting console cannot know the request exists — it would have to replay
unanswered inbound requests or synthesise denials.

**That cannot happen in this deployment.** The session is launched with
`permission_mode="bypassPermissions"` and `setting_sources=[]`, registers no hooks and no
`can_use_tool`, and reaches MCP as an external HTTP server the CLI contacts itself rather than
through a client-hosted server. So the control channel here is effectively outbound-only:
`initialize` once, `interrupt` on abort.

This removes the hardest part of B before it starts — and it is a property that can be lost by
accident. **Adding a hook, a `can_use_tool` callback, or a client-hosted MCP server makes
re-adoption qualitatively harder**, and whoever adds one should come back to this note.

**Called in once, and the warning above turned out to be too broad.** The Matrix read tools
were designed against a client-hosted server (<matrix_chat_runtime.md> R5.2a). Working through
it sharpened what the hard part actually is:

**The problem is not inbound control traffic. It is an unanswered inbound request whose
replay has side effects.** The runner is already buffering and redialling for B, so it can
equally hold the `control_request`s nobody answered and re-deliver them on adopt, along with
everything else past the cursor — the adopting console answers them late and the CLI never
knew. That works whenever answering twice is harmless, which covers a read-only MCP surface
completely. It does **not** cover `can_use_tool` or a hook: those gate an action, a replayed
approval is a second authorization, and a synthesized denial silently changes what the turn
did. So read-only client-hosted tools cost buffering work; permission callbacks and hooks are
the ones that are qualitatively harder, and they are what the paragraph above should be read
as warning about.

R5.2a still lands on plain HTTP entries, but on grounds that have nothing to do with this
note: the scoping client hosting would have bought is explicitly not wanted (R5.3a), so it is
buffering work against no benefit. R5.5a takes the same reasoning the other way, persisting
the rollout as **wire frames** rather than parsed objects, precisely so design B's "read the
stream directly for an adopted turn" does not turn the store into a migration.

### What B needs

- **The runner owns the `initialize` handshake.** It is per-connection state today, but
  the CLI now outlives the connection, so an adopting console must not re-handshake a process
  that is already initialized. The runner owns the process lifetime, so it is the honest owner
  of the fact that the handshake happened. Consoles come and go around it.
- **A resume point, made safe by identity rather than by exactness.** The runner keeps a bounded
  buffer of frames it has not seen acknowledged and re-sends from there on adopt. See below: what
  makes that correct is that a replayed frame is recognisable, not that the cursor is right.
- **An adopt path in `authenticate_bridge`.** It currently requires `status == PROVISIONING`
  and `bridge_connected_at is None`, so every reconnect is refused. Adoption must be gated on
  taking the lease — that is the arbitration that stops two replicas adopting one CLI.
- **Routing an adopted turn by turn.** `_run_turn` assumes this process issued the prompt it is
  draining. An adopted mid-flight turn has to be picked up off the stream instead, and routed by
  _turn_ rather than by session, since a session outlives many of them — which is what
  `claude_chat_turns` brackets.
- **An idle timeout in the runner.** A CLI held open for a console that never returns trades a
  wedged room for a wedged sandbox.

### Replay is safe because frames have identity — with one exception

The first version of the resume point above wanted an exact, durable cursor: a monotonic sequence
number on the envelope, and an acknowledgement the runner could trust. That is the expensive part —
it is a `PROTOCOL_VERSION` bump, which is not atomic across two independently rolled images — and it
is also the wrong thing to lean on. **A cursor cannot be the correctness argument, because the
console can die between recording a frame and acknowledging it**; the frame is then replayed no
matter how exact the cursor was. Something downstream has to tolerate seeing it twice.

Once it does, the cursor stops being load-bearing: it bounds _how much_ is replayed, not whether
replaying is safe. Deliver at least once, recognise duplicates, and the sequence number becomes an
optimisation that can be added later or not at all.

**The frames the console keeps already carry identity**, assigned by the agent rather than by us:

| frame               | identity                                                              |
| ------------------- | --------------------------------------------------------------------- |
| `assistant`         | `message.id` (`msg_…`) — already stored as `agent_message_id`         |
| `user`              | the `tool_use_id` of the `tool_result` block it carries; one per call |
| `result`            | one per turn, and the console knows which turn is open                |
| `command_lifecycle` | `(command_uuid, state)` — the uuid we stamped, plus which state it is |
| `system/task_*`     | `task_id`, and `tool_use_id` where one applies                        |

**The exception is `stream_event`, and it is the one place replay actively corrupts.** A delta has
no identity — two identical ones are legitimately distinct — and the turn loop's `streamed += delta`
would double-append the text on replay. But deltas are also the one class that never needs
replaying: each is a preview of an `assistant` frame that arrives complete moments later, and the
console already declines to record them (`RolloutRecorder` skips them, so the log stays readable).
So the buffer's rule falls out of a rule that already exists for another reason: **the runner buffers
everything except deltas.** Both halves of the system then agree that a delta is worth nothing once
its message has landed.

Two consequences worth building deliberately:

- **Make it a schema property, not a rule.** A nullable `frame_uid` on `claude_chat_frames` with a
  partial unique index on `(session_id, frame_uid)` makes "the same frame twice" unrepresentable
  rather than something every writer has to remember — the shape `uq_claude_chat_turns_open` and
  `uq_claude_chat_prompts_unclaimed` already use. Additive.
- **Dedupe where the frame enters, not where it is stored.** The record is not the only thing a
  frame touches: a replayed `assistant` frame that reached `_run_turn` would post the message into
  the room a second time. The check belongs at ingestion, ahead of dispatch, so a duplicate is
  neither recorded nor acted on. Matrix offers a second line for free — `send_text` currently passes
  `txn_id=uuid4().hex`, so the homeserver cannot deduplicate a reply it has already seen. Derived
  from the message's own `msg_…` id it could. (The reasoning that keeps _notices_ on fresh
  transaction ids does not apply here: it is about derived counters resetting across a restart, and
  an agent-assigned message id does not reset.)

### The bridge protocol is versioned; the versioning is pointed the wrong way

`PROTOCOL_VERSION` is 2, it rides on the `start` frame, and `ClaudeLaunch.protocol_version` is
`Literal[2]` so a peer on another version fails validation immediately rather than further in with a
stranger symptom. That is a real version, and it was the right one for a bridge whose two ends live
and die together. Re-adoption breaks three of its assumptions at once.

**It flows one way — from the end that cannot adapt to the end that must.** `start` is
console → runner, so the runner validates the console's version and the console never learns the
runner's. Today that is harmless, because a rejected session is replaced within ninety seconds by
one launched from the current image. Under adoption it inverts: the runner's image is fixed when its
claim is created and its process now outlives many console releases, while the console rolls six
times a day. The end that has to be backward compatible is the console, and it is the end flying
blind. The runner has to state its version — on connect, and again on adopt, since an adopting
console did not launch it.

**Exact match has no room to negotiate.** `Literal[2]` admits one value; there is no
minimum-supported, no range, no "speak 2 to this peer and 3 to that one". A console that must serve
both an old runner and a new one cannot, so the first release after this ships would kill every
session older than itself — which is the thing being fixed.

**`extra="forbid"` makes every additive field a fleet-wide breaking change.** That is deliberate and
its reasoning is stated in `_Frame`: a frame this end does not fully understand is a version
mismatch, and silently dropping an unknown field would let the two ends disagree about what was
said — the cost being honest, since no frame change is atomic across two independently rolled
images. Correct today. But the replay design above adds a field to the envelope, and under adoption
"not atomic" stops meaning "a few sessions fail during the rollout" and starts meaning "every live
session dies on every console release that touches the envelope".

**The rule that gets both properties: evolve by adding kinds, not fields.** The envelope already
discriminates on `kind`, so an unknown _kind_ fails the union parse — fail-closed, for free, exactly
where a must-understand change belongs. An optional field added to a known kind is safe to ignore
precisely because a receiver that ignores it behaves as it did before, which is the old version's
correct behaviour. So:

- unknown `kind` → reject, as now;
- unknown field within a known kind → ignore rather than reject, which is what turns an additive
  change from a fleet-wide break into a no-op for peers that predate it;
- anything a peer **must** understand to stay correct arrives as a new kind, not as a field — the
  adopt/resume frame this design needs is itself an example, and it is naturally fail-closed;
- the version stops being an assertion and becomes a negotiation: the runner offers what it speaks,
  the console picks the highest both know.

`SetupOutput` is the precedent that this works — it exists because the envelope has somewhere to put
a frame belonging to neither protocol, which is the same property being leaned on here.

#### A supported range, and what it costs to have one

The console keeps a range — `SUPPORTED = 2..N` — rather than a number, and speaks whatever a given
runner speaks. Right, and four things follow that are easier to build in than to retrofit.

**A range only stays cheap if unknown fields are ignored on receipt.** The console does not merely
parse frames, it emits them, so "supporting v2" means being able to _produce_ v2-shaped frames. With
`extra="forbid"` on the receiving end, a v2 runner rejects any frame carrying a field it predates —
so a console supporting 2..4 would have to keep a serializer per version and pick one per
connection. Relax forbid to ignore on receipt and that collapses: the console emits its newest
shape, an older peer drops what it does not know, and behaves as its version correctly did. The
range and the field policy are the same decision, not two.

**Negotiation needs a fixed point, and today there is none.** The version rides on `start`, which is
console → runner and the _first_ frame — so the console must choose a version before it has heard
anything, and the runner cannot state its own until after it has decoded a frame whose shape is what
is in question. The way out is that the runner speaks first: a minimal `hello` carrying nothing but
its supported range, whose shape is then **frozen forever**, with the console replying `start` (a new
session) or `resume` (an adoption) in the version it picked. Every negotiated protocol needs one
frame that can never change; this is the moment to choose it and keep it as small as it can be.

**Installing that is itself the last breaking change.** A v2 runner waits for `start` and rejects an
unknown kind, so it will never send or accept a `hello`. The transition is a shim on the console
side: wait briefly for a `hello`, and on silence assume a pre-negotiation peer and send the v2
`start` it is waiting for. Bounded, deletable, and worth writing with the deletion condition in the
comment — once no live session runs a runner image older than the negotiation, the shim goes. One
more flag day buys the end of flag days.

**Dropping a version from the low end is an operational step, not an edit.** Removing 2 from the
range means a sandbox still speaking 2 can no longer be adopted by any console — so it has to be
drained first, and "is anything still on it" is answerable: a session's runner image is fixed at
claim creation, so the question is whether any live session predates that image. The same reasoning
as the expand/contract migrations, with the roll being the runner fleet rather than the console's.

**And a range needs a test per end of it.** This repo already runs the discipline for FastMCP — one
exact version, adapter contract tests before a repin. A range inverts it: the contract tests run at
the oldest and the newest supported version, or "we support 2" quietly becomes a claim nobody
checks. `test_transport.py` and `test_runner.py` are where that matrix goes.

### The lifecycle opens a protocol horizon

A session's outer bound is no longer a fixed `shutdownTime`: the console slides the SandboxClaim's
deadline while the session is tended and deletes the claim on a clean end
(<matrix_chat_runtime.md> R3.2a/b, <chat_runtime_cleanup.md>), so a conversation in full flow no
longer dies on a clock. What that leaves for this document is the protocol consequence.

**A tended session now has no upper bound on its lifetime, and that bound was the
protocol-compatibility window.** A runner's image is fixed when its claim is created, so the oldest
live runner is exactly as old as the longest-lived session — exactly how far back the console must
still speak the bridge protocol. Under the old fixed TTL with a janitor above it the window was
finite and derivable: at roughly six console releases a day, a 24h horizon meant the last day's
runner images. With the deadline slid and no janitor, "the console must remain compatible with every
bridge version ever shipped" is the policy unless a bound is chosen. Pick the horizon deliberately —
bound the session lifetime, or version the bridge protocol so an old runner degrades rather than
breaks — and derive the support window from it, rather than discovering it when an eight-month-old
sandbox refuses a handshake.

### Open questions

- How large a ring buffer is enough, and what should the runner do when it overflows —
  drop the session, or drop frames and let the console reconcile from its own persisted
  messages?
- Should an adopting console re-announce anything to the room, or is a silent recovery the
  better behaviour? A room that says nothing when nothing was lost is arguably correct, but it
  makes the mechanism invisible when it is new and still being trusted.
- Does `--resume` (design A) belong as the fallback when adoption fails, giving two tiers of
  recovery before a session is declared lost?
