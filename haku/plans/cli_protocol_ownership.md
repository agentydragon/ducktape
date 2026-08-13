# Owning the CLI protocol

**Status: decided, not built** (2026-08-12). The console drives Claude Code's newline-delimited
JSON protocol itself, keeping the Agent SDK only for launch-argument construction. This note
holds that decision and the session re-adoption design it is a prerequisite for — one document
because the two are the same seam seen from different sides.

## Why stop going through the SDK

The SDK earns its place when it is the thing that knows the protocol. Here it is increasingly
the thing standing between us and it, and four separate needs now point at the same seam:

- **It cannot ask for the lifecycle events.** `command_lifecycle` — the difference between
  confirming a mid-turn steer landed and inferring it from what the model then does — is
  emitted only for an inbound user frame that carries a `uuid`, and the SDK never sends one.
  Probed against the binary and then run: adding it produces `queued` → `started` →
  `completed`, with the fold visible as `completed` arriving **before** the turn's `result`
  (<../cli_protocol/probes/steering.py>).

- **Its typed layer is already not our source of truth.** `Message` has no variant for
  `command_lifecycle`, `system/task_started`, `task_notification` or `transcript_mirror`;
  `ThinkingBlock` is on the wire and dropped by our extraction; a result's cost and usage are
  read for an error check and discarded. The rollout store keeps raw frames precisely because
  of this, so the SDK is parsing into objects the record does not use.
- **`receive_response()` is the wrong shape.** It is request-scoped and assumes this process
  issued the turn. That blocks the fold path — writing a prompt into a response we are already
  draining — and it is what design B below calls "the one place the SDK offers nothing".
- **We already own everything around it**: the transport, the envelope, the runner, the frame
  store. What is left is a thin protocol client.

**What stays: nothing.** This originally kept launch-argument construction — flags, MCP
configuration, the system-prompt preset shape — as "dull, churn-prone, and genuinely someone
else's problem". That was wrong on the arithmetic. `SubprocessCLITransport._build_command()`
translates ~40 options and the console sets seven; the rest were branches we never took, on a
**private** method of a transport we constructed purely to borrow it and never let connect,
assigning `_cli_path` from outside to make it work. `options.py` now builds the argv from a
frozen `ClaudeSession` of those seven, `test_options.py` pins the exact result, and the pinned
result was run against a real CLI.

The wheel remains a **build** dependency for one reason: it bundles the `claude` binary the
runner image needs. No Python imports it.

**That last thread should be pulled too — [later].** Anthropic publishes the CLI as
`@anthropic-ai/claude-code` (481 versions, per-platform binaries such as
`@anthropic-ai/claude-code-linux-x64`), so sourcing it from a Python wheel is not necessity but
habit, and a worse habit than it looks: `extract_claude.py` reaches into another package's
`_bundled/` for a file it happens to ship, and the CLI version is then pinned only as a side
effect of the SDK's. npm is the real distribution channel, this repo already manages npm through
`@aspect_rules_js` and pnpm, and pinning the CLI directly is what the version-pinning discipline
in <../cli_protocol/README.md> actually asks for. Cost is a `package.json` entry, a
`js_binary`-or-`filegroup` in place of the `claude_executable` genrule, and deleting
`extract_claude.py`.

**What it costs.** Owning protocol breakage across CLI upgrades. Two things make that
affordable rather than reckless: the CLI ships _bundled with_ the SDK, so the pairing is pinned
either way, and this repo already runs that discipline for FastMCP (one exact version, adapter
contract tests before a repin). Tests are cheap here — a fake CLI answering scripted frames is
a few dozen lines, as the probe's smoke test showed.

## Order of work

Each step is useful on its own and none of them is a rewrite.

1. **Read the frame stream ourselves**, dispatching by frame type instead of calling
   `receive_response()`. Unblocks mid-turn folding (R2.2a) and is a prerequisite for adoption.
   The frames already pass through the client's own reader, which is also where they are
   recorded; this is about who routes them.
2. **Own `initialize`.** Request/response correlation is a dict of futures, a counter and a
   timeout. Done with step 1, since the blocking buffer makes them one change.
3. **Own `interrupt`**, which is then a few lines on step 2's machinery — and lets abort
   reason about `interrupt_cancel_queued_v1` rather than assume interrupt and queued messages
   do not interact.
4. **Type only the frames we act on**, with our own models. Everything else is archived raw,
   which is already the design (R5.5a).

After step 4 the SDK is a launch-args helper, and removing it entirely is a separate small
decision rather than the point of the exercise.

## What owning the handshake buys — [later]

The protocol reference is <../cli_protocol/README.md>; field shapes, measured behaviour and the
probes that establish them live there and are not repeated here. This is only the judgment about
which of it Haku should take up, and it is [later] work — none of it blocks the steps above.

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

**Status: not built.** Recorded while the protocol details were in hand; step 1 above is its
first prerequisite.

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

### The protocol B has to survive

Newline-delimited JSON over the CLI's stdin/stdout, which the bridge already carries verbatim
as `ClaudeMessage.payload`. **Two channels are multiplexed on it**, distinguished by the
top-level `type`.

**Conversation.** SDK → CLI is a single shape:

```json
{ "type": "user", "session_id": "", "message": { "role": "user", "content": "…" }, "parent_tool_use_id": null }
```

CLI → SDK is `assistant`, `user`, `system`, `result`, `stream_event`, plus `task_started` /
`task_progress` / `task_notification`, `rate_limit_event`, `server_tool_use`,
`advisor_tool_result`, and `transcript_mirror`. Append-only, which is what makes replay
tractable.

**Control.** Request/response correlated by `request_id`:

```json
{ "type": "control_request", "request_id": "…", "request": { "subtype": "interrupt" } }
{ "type": "control_response", "request_id": "…", "response": { "subtype": "success" } }
{ "type": "control_cancel_request", "request_id": "…" }
```

It is **bidirectional** — both ends originate requests. SDK → CLI covers `initialize`,
`interrupt`, `set_model`, `set_permission_mode`, `get_context_usage`, `mcp_status`,
`mcp_toggle`, `mcp_reconnect`, `rewind_files`, `stop_task`. CLI → SDK covers `can_use_tool`,
hook callbacks, and calls into SDK-hosted MCP servers.

### Gotcha: inbound control traffic is what normally makes this hard, and we have none

`Query.pending_control_responses` is an in-memory `request_id` → Event map. If the CLI has an
outstanding request to the SDK when the console dies, nobody answers it, the CLI blocks
forever, and an adopting console cannot know the request exists — it would have to replay
unanswered inbound requests or synthesise denials.

**That cannot happen in this deployment.** The session is built with
`permission_mode="bypassPermissions"` and `setting_sources=[]`, registers no hooks and no
`can_use_tool`, and reaches MCP as an external HTTP server the CLI contacts itself rather than
through an SDK-hosted server. So the control channel here is effectively outbound-only:
`initialize` once, `interrupt` on abort.

This removes the hardest part of B before it starts — and it is a property that can be lost by
accident. **Adding a hook, a `can_use_tool` callback, or an SDK-hosted MCP server makes
re-adoption qualitatively harder**, and whoever adds one should come back to this note.

**Called in once, and the warning above turned out to be too broad.** The Matrix read tools
were designed against an SDK-hosted server (<matrix_chat_runtime.md> R5.2a). Working through
it sharpened what the hard part actually is:

**The problem is not inbound control traffic. It is an unanswered inbound request whose
replay has side effects.** The runner is already buffering and redialling for B, so it can
equally hold the `control_request`s nobody answered and re-deliver them on adopt, along with
everything else past the cursor — the adopting console answers them late and the CLI never
knew. That works whenever answering twice is harmless, which covers a read-only MCP surface
completely. It does **not** cover `can_use_tool` or a hook: those gate an action, a replayed
approval is a second authorization, and a synthesized denial silently changes what the turn
did. So read-only SDK-hosted tools cost buffering work; permission callbacks and hooks are
the ones that are qualitatively harder, and they are what the paragraph above should be read
as warning about.

R5.2a still lands on plain HTTP entries, but on grounds that have nothing to do with this
note: the scoping SDK hosting would have bought is explicitly not wanted (R5.3a), so it is
buffering work against no benefit. R5.5a takes the same reasoning the other way, persisting
the rollout as **wire frames** rather than SDK objects, precisely so design B's "read the
stream directly for an adopted turn" does not turn the store into a migration.

### What B needs

- **The runner owns the `initialize` handshake.** It is per-connection state in the SDK, but
  the CLI now outlives the connection, so an adopting console must not re-handshake a process
  that is already initialized. The runner owns the process lifetime, so it is the honest owner
  of the fact that the handshake happened. Consoles come and go around it.
- **A resume point.** The runner keeps a bounded ring buffer of frames it has not seen
  acknowledged; on adopt, the console says which sequence it already has. That means a
  monotonic sequence number on runner → console frames — additive to the envelope, and by the
  envelope's own `extra="forbid"` rule it costs a `PROTOCOL_VERSION` bump.
- **An adopt path in `authenticate_bridge`.** It currently requires `status == PROVISIONING`
  and `bridge_connected_at is None`, so every reconnect is refused. Adoption must be gated on
  taking the lease — that is the arbitration that stops two replicas adopting one CLI.
- **Reading the stream directly for an adopted turn.** `client.query()` + `receive_response()`
  is request-scoped and assumes this process issued the turn. An adopted mid-flight turn has to
  be read off the transport and routed by turn — this is step 1 of the ownership work above,
  and the reason it is first. Routed by _turn_ rather than by session, since a session outlives
  many of them — which is what `claude_chat_turns` brackets.
- **An idle timeout in the runner.** A CLI held open for a console that never returns trades a
  wedged room for a wedged sandbox.

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
