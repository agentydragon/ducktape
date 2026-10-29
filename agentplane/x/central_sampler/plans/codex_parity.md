# Codex feature parity

Which Codex `rust-v0.162.1` features the central sampler ports, defers or leaves out. The sampler
already has the provider loop, code mode, compaction, interrupt, model/effort changes, standing
instructions, setup scripts and History export.

## Port

- **Mid-turn steering:** deliver user input submitted during an active turn at the next boundary,
  as Codex does, instead of queueing it until the turn ends.
- **Environment context:** Codex's `<environment_context>` with cwd, the executor-reported shell and
  the current date. Timezone deferred.
- **Reasoning summaries:** request them like Codex, so History can show reasoning.

## Later

- **Guardian-style automatic approval review**, built at Agentplane's level for every harness, not
  as a Codex-specific port.
- **Agent access to sampler persistence:** let agents read retained conversation and evidence
  records, with explicitly granted privileges.
- **Actions as tools:** expose Action Service Actions (including MCP-backed ones) as nested tools,
  deferred behind tool search like Codex's MCP tools. Needs design first: what happens when the set
  of available Actions changes mid-Session, and how agents learn of additions and removals.
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
- Very low priority: undo of turns.
- Context-awareness tools, if wanted: Codex's `get_context_remaining`, `new_context_window`, and
  token-budget and current-time reminders in context.

## Not porting

- Sandboxing, execpolicy, approvals and the network proxy: the Sandbox, egress proxy and Action
  Service own these boundaries.
- Codex local persistence (rollout files, thread store, state DB): PostgreSQL and History replace it.
- Turn diffs, review mode, desktop in-app panes and fast mode.
- Codex plugins, apps and connectors, unless specifically wanted later.
- Image generation: a different API; if wanted, an Agentplane Action instead.
- Shell plumbing (shell snapshots, zsh fork, login-shell PATH, PowerShell), worktree management and
  external-agent migration.
- TUI, cloud tasks and ChatGPT-backend features (login/keyring, PSP, plan analytics, access
  programs, request compression).
- Local model providers (LiteLLM routes models), Codex's OTel exporter, feedback upload and Windows
  support.
