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

**Status: not built.** The protocol ownership it waited on has landed, so nothing above it blocks
it now.

Today a console replica that dies takes its Claude Code session with it. The lease
(<../console/x/claude_chat.py>, `expire_stale_leases`) makes that **observable** — another
replica notices the holder stopped renewing and fails the session instead of leaving the room
waiting forever. This section is about the next step: making the session **survive** the roll
rather than merely being cleaned up after it.

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
in-flight turn. This is the target.

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

### Letting the console own the whole lifecycle

Today a session's outer bound is `shutdownTime` on the SandboxClaim, set to
`now + session_ttl_seconds` (7200) **at creation and never patched**. So it is not an idle timeout —
a conversation in full flow dies at exactly two hours, mid-turn, and the room is told the session
failed. Removing it in favour of console-managed lifecycle is right, and cheaper than it looks
because the TTL is not what prevents leaks: the Kyverno `CleanupPolicy` beside the template already
reaps Sandboxes and SandboxClaims older than 24h, at the CR layer, precisely because the Agent
Sandbox controller recreates deleted Pods. That janitor is the backstop; the TTL is a policy on top
of it.

So: drop `shutdownTime` to the janitor's horizon or omit it, and let the console release a sandbox
when it decides the session is done — an idle timer, plus the lease as liveness, plus the janitor as
the thing that catches a console that forgot both.

**Order matters here.** Do not remove the TTL before rolls are survivable. Today it is quietly
recycling sessions that wedged — a session whose runner is crashlooping into a refused reconnect is
reclaimed by the TTL, not by anything that understands what happened
(<../console/debug/2026_08_13_sessions_boot_and_die.md>). Remove the backstop first and those
sessions stop being cleaned up at all.

### The lease decision this revisits

`expire_stale_leases` currently treats an expired lease as **dead**: fail the session, sweep the
claim, provision a replacement. Re-adoption wants it to mean **unowned** — adoptable, with
failure only once adoption has not happened (or has been tried and failed).

That is a semantic change to one method rather than a rewrite, but it is the part of the lease
work that was decided before re-adoption existed, so it is where to start.

### Open questions

- How large a ring buffer is enough, and what should the runner do when it overflows —
  drop the session, or drop frames and let the console reconcile from its own persisted
  messages?
- Should an adopting console re-announce anything to the room, or is a silent recovery the
  better behaviour? A room that says nothing when nothing was lost is arguably correct, but it
  makes the mechanism invisible when it is new and still being trusted.
- Does `--resume` (design A) belong as the fallback when adoption fails, giving two tiers of
  recovery before a session is declared lost?
