# Codex feature parity

Codex `rust-v0.162.1` features the central sampler has not ported yet. Ported behavior is in
[SPEC.md](../SPEC.md); gaps with a code site are `TODO`s in the code (timezone, base-instruction pruning,
steering stream cuts and pending-input ordering, cell resource limits). Features left out on purpose
are in the [README](../README.md#codex-features-left-out).

## Later

- **Guardian-style automatic approval review**, built at Agentplane's level for every harness, not
  as a Codex-specific port.
- **Agent access to sampler persistence:** let agents read retained conversation and evidence
  records, with explicitly granted privileges.
- **Actions as tools:** expose Action Service Actions (including MCP-backed ones) as nested tools,
  deferred behind tool search like Codex's MCP tools. Needs design first: what happens when the set
  of available Actions changes mid-Session, and how agents learn of additions and removals.
  Coordinate provider-visible changes with the [Anthropic dynamic-tool follow-up](anthropic.md#dynamic-tool-definitions).
- **Skills (medium priority):** Codex `SKILL.md` discovery and injection. Needs design first: where
  skills live and how their files reach the execution environment.
- **Subagents and multi-agent:** build as Agentplane-level cross-Session messaging with privileges,
  not a port. Use Codex multi-agent v2 and its agent message board, and Claude Code's subagents, as
  reference designs.
- **Memories:** Codex-style memory extraction and consolidation across Sessions.
- **Goals:** persisted Session goals with automatic continuation.
- **Session fork:** branch a Session's conversation into a new Session. Needs its own design across
  the sampler, History and the executor binding.
- **Hosted web search:** maybe, depending on what the provider's hosted search offers over fetching
  from the Sandbox.

## Low priority

- Realtime/voice.
- Browser use, computer use and dictation.
- `update_plan`: only as an Agentplane-wide feature, not a sampler-only tool.
- Maybe, very low priority: lifecycle hooks (`hooks.json`), and `request_user_input` with async
  messages to the user.
- AGENTS.md: Codex's root-to-cwd chain (one per Session, since the cwd never changes), and possibly
  Claude-style nested discovery triggered by nested tool paths, which the sampler sees directly.
- Very low priority: undo of turns.
- Context-awareness tools, if wanted: Codex's `get_context_remaining`, `new_context_window`, and
  token-budget and current-time reminders in context.
