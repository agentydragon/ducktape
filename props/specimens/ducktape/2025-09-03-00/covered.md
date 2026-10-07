1. “Avoid broad try/except around instruction_block / config loading” (draft: iss-018)

- Location pointers: src/adgn_llm/mini_codex/cli.py (instruction_block sites ~199–204, ~285–290), and config load
  ~343–346.
- What we planned to flag: broad excepts (except Exception: pass) silently hide programming/shape bugs and should be
  replaced with explicit capability handling or narrow catches + logging.
- Why we backed off: the instruction_block call and how server-level description text should be surfaced are a coupled
  design question. Replacing caller-side defensiveness without deciding whether instruction_block/init_result should
  exist risks adding churn or creating a wrong, brittle local fix.

2. “McpManager missing instruction_block capability / server-level description not injected” (draft: iss-021)

- Location pointers: src/adgn_llm/mini_codex/mcp_manager.py (where manager session/init exists) and calling sites in
  agent/CLI.
- What we planned to flag: callers call mcp_manager.instruction_block() but McpManager does not implement it; this is a
  capability mismatch and leads to AttributeError (currently masked).
- Why we backed off: this is a design decision, not a one-line bugfix. There are multiple valid approaches (add a no-op
  instruction_block on McpManager, read slot.init_result and inject it, or introduce a typed Protocol). We should choose
  the intended API before filing the issue so the guidance is prescriptive and actionable rather than speculative.

Why we removed those two drafts

- They were coupled: the broad-except change and the missing-capability change are two sides of the same design. Without
  a clear decision about how server-level descriptive text should be provided to the agent (instruction_block vs
  init_result vs explicit caller wiring), issuing prescriptive fixes would either be incomplete or push the code toward
  a particular architecture prematurely.
- We prefer a small, explicit design decision first. Once the direction is chosen, we can add one concise issue (or a
  minimal patch) that is correct and reviewable.

"Temporarily backed off on two MCP-level issues: (1) replacing broad excepts around instruction_block/config loading,
and (2) the missing instruction_block capability on McpManager. These two changes are tightly coupled to how
server-level descriptive text should be surfaced to the agent (instruction_block vs init_result). We deferred filing
actionable issues until the team decides the intended API; once that decision is made we will add one focused issue (or
a minimal patch) that implements the agreed approach."
