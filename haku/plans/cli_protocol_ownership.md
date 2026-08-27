# Owning the CLI protocol

**Status: built** (decided 2026-08-12). The console drives Claude Code's newline-delimited JSON
protocol itself: <../cli_protocol/frames.py> types the control channel, `ClaudeCli`
(<../console/x/claude_code/client.py>) reads both channels and owns `initialize` and `interrupt`,
and `runtime/x/bridge/claude_options.py` builds the launch argv. No Python imports the Agent SDK. Why each of
those is ours rather than the SDK's is written where it is now maintained — those modules'
docstrings — and the wire itself is <../cli_protocol/protocol.md>.

The conversation channel stays deliberately unmodelled: the console's record of a session is the
wire, and a frame gets a model when the code that acts on it exists.

Session re-adoption across a console roll — the design this decision was a prerequisite for — is
also built; its decision record, including the replay hazard and the rejected alternatives, is
<../runtime/x/bridge/docs/design.md>.

What is left here is what that decision did not finish — where the CLI binary comes from, the
capabilities owning the handshake put within reach, and the protocol-horizon policy the session
lifecycle opens.

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
  tool implementation stays where the data already is. The Matrix read tools passed on this when
  it bought structural session scoping that was then decided against — reads are unscoped on
  purpose (<../console/x/channels/matrix/SPEC.md> § The agent's own view) — but as a way to give
  Haku console-side tools it stands on its own. This is the strongest candidate on the list
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
  decision record (<../runtime/x/bridge/docs/design.md>) warns about: the hazard is an unanswered
  request whose **replay has side effects**, which a permission hook has and a read-only one does
  not. Read that first.

Two to know about without acting on:

- **`supportedDialogKinds`** fails closed, so we are already taking the degraded path silently —
  for `refusal_fallback_prompt`, the classic refusal error. Whether that is wrong depends on
  whether a Matrix room can host a blocking dialog at all, which is a surface question.
- **`toolAliases`, `planModeInstructions`, `excludeDynamicSections`, `title`,
  `agentProgressSummaries`** are accepted and unmeasured or inert here. `initialize` validates
  almost nothing, so any of them can be set in the belief it did something; check for an effect
  rather than for an error.

## The lifecycle opens a protocol horizon

A session's outer bound is no longer a fixed `shutdownTime`: the console slides the SandboxClaim's
deadline while the session is tended and deletes the claim on a clean end, so a conversation in
full flow no longer dies on a clock. What that leaves for this document is the protocol consequence.

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

## Open questions

- How large a ring buffer is enough, and what should the runner do when it overflows —
  drop the session, or drop frames and let the console reconcile from its own persisted
  messages?
- Should an adopting console re-announce anything to the room, or is a silent recovery the
  better behaviour? A room that says nothing when nothing was lost is arguably correct, but it
  makes the mechanism invisible when it is new and still being trusted.
- Does `--resume` (design A in <../runtime/x/bridge/docs/design.md>) belong as the fallback when
  adoption fails, giving two tiers of recovery before a session is declared lost?
