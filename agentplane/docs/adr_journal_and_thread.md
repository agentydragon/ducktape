# ADR: Journal and Thread

- **Status:** Accepted (operator, 2026-11-14 UTC: "naming: sg"; the Session names on 2026-11-14
  20:32 UTC, card "Add the native session naming rule to the Journal and Thread ADR?": "Add the
  rule"). Names only; no migration is decided ([`JOURNAL_REPRESENTATION_DESIGN`](../plans/task_dag.md#journal_representation_design--history-as-stored-journals)).
- **Scope:** Agentplane vocabulary in code, APIs, docs and the UI

## Context

"Thread" names the History fold's conversation-style projection of a Session (`thread_*` tables,
`ReadThreadEntities`; [thread layering](thread_layering.md#authorities-and-representations)).
The operator wants two things kept apart by name: the logs we get from a harness or process, and
the messaging object, with editable messages, that agents and people act on.

Inside an Agentplane Session, a hosted harness keeps its own session object (`native_session_id`: a
Claude session ID or Codex thread ID), and the code-mode Executor its own (`execution_session_id`).
Their IDs are already distinct from the Session ID, and one Session can span several of them: a
harness restarted before its first turn starts a new one, the central sampler opens a fresh
execution session after a definite loss, and native subagents are further native sessions, not
Agentplane Sessions
([runner delivery design](runner_delivery_and_harness_design.md),
[session protocol](../env/runtime/docs/session_protocol.md#harness-originated-messages)).

## Decision

- **Journal:** what a harness or process emits, stored verbatim with its kind. History heads toward
  being the Journal service; the app shows a **journal view** of a Session, a rendering of its
  Journal rather than a conversation.
- **Thread:** the messaging object with editable messages that agents and people act on.
- The projection meaning of "Thread" goes away with the fold. Until then, new code, APIs and docs
  do not use "Thread" for it.
- "Conversation" names neither: the central sampler uses it for model context.
- **Session:** unqualified, always the Agentplane Session. Its ID is the only address.
- **Native session:** a hosted harness's own session object. **Execution session:** the code-mode
  Executor's. Outside code that speaks that harness's or the Executor's protocol, neither is called
  a bare "session" or "thread".
- Native and execution session IDs belong to the object they name. Only the code that speaks its
  protocol uses one (the Runtime's harness adapter; the central sampler's Executor client); status
  and diagnostics may show it. Nothing addresses, routes or keys an Agentplane Session by
  either.
