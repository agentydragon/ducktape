# Codex feature parity

Which Codex `rust-v0.162.1` features the central sampler ports, defers or leaves out. The sampler
already has the provider loop, code mode, compaction, interrupt, model/effort changes, standing
instructions, setup scripts and History export.

## Port

- **Mid-turn steering:** deliver user input submitted during an active turn at the next boundary,
  as Codex does, instead of queueing it until the turn ends.

## Later

- **Guardian-style automatic approval review**, built at Agentplane's level for every harness, not
  as a Codex-specific port.
- **Agent access to sampler persistence:** let agents read retained conversation and evidence
  records, with explicitly granted privileges.

## Low priority

- Realtime/voice.
- Browser use, computer use and dictation.

## Not porting

- Sandboxing, execpolicy, approvals and the network proxy: the Sandbox, egress proxy and Action
  Service own these boundaries.
- Codex local persistence (rollout files, thread store, state DB): PostgreSQL and History replace it.
- Shell plumbing (shell snapshots, zsh fork, login-shell PATH, PowerShell), worktree management and
  external-agent migration.
- TUI, cloud tasks and ChatGPT-backend features (login/keyring, PSP, plan analytics, access
  programs, request compression).
- Local model providers (LiteLLM routes models), Codex's OTel exporter, feedback upload and Windows
  support.
