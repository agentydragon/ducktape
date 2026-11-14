# ADR: Journal and Thread

- **Status:** Accepted (operator, 2026-11-14 UTC: "naming: sg"). Names only; no migration is
  decided ([`JOURNAL_REPRESENTATION_DESIGN`](../plans/task_dag.md#journal_representation_design--history-as-stored-journals)).
- **Scope:** Agentplane vocabulary in code, APIs, docs and the UI

## Context

"Thread" names the History fold's conversation-style projection of a Session (`thread_*` tables,
`ReadThreadEntities`; [thread layering](thread_layering.md#authorities-and-representations)).
The operator wants two things kept apart by name: the logs we get from a harness or process, and
the messaging object, with editable messages, that agents and people act on.

## Decision

- **Journal:** what a harness or process emits, stored verbatim with its kind. History heads toward
  being the Journal service; the app shows a **journal view** of a Session, a rendering of its
  Journal rather than a conversation.
- **Thread:** the messaging object with editable messages that agents and people act on.
- The projection meaning of "Thread" goes away with the fold. Until then, new code, APIs and docs
  do not use "Thread" for it.
- "Conversation" names neither: the central sampler uses it for model context.
