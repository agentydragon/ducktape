# Agentplane typing audit

Requested during review of PR #13428: scan that whole PR and independently scan
Agentplane on devel. Prefer idiomatic concrete types over stringly control flow.

## Pinned inputs

- PR #13428: `80a68a83950b35583740a741b33d39f08e96d2fb`, merge base
  `77a8b14e4832a428658bc4591f3c3f93c18ae431`; all ten changed files and affected
  producers/consumers.
- Devel: `3531546cee7b2d8344a072288854ad13dfd2bbbc`; tracked `agentplane/**`.

## Assignments

- `pr_13428_typing_audit`: entire PR; findings use `PR-T` IDs.
- `devel_agentplane_typing_services`: every Agentplane path except the runtime/UI
  paths below; findings use `DEV-S` IDs.
- `devel_agentplane_typing_runtime_ui`: `env`, `x`, `native`, `runner`,
  `runner_transport`, `proto`, `system_tests`, `testing`, and `app/frontend`;
  findings use `DEV-R` IDs.

All three reviewers use GPT-6 Luna at xhigh effort. The devel assignments partition
the source tree; cross-boundary findings name both the producer and consumer.

## Review criteria and deliverables

Inspect existing generated/proto/SDK enums, repeated domain vocabularies, method
names, untyped maps driving decisions, silent assertions/defaults, duplicated
schemas and invalid optional combinations. Prefer existing owners, structs,
named types/constants and typed variants. Preserve legitimate open external
payloads, raw evidence, unknown variants, nulls, integer precision, and distinct
request/process outcomes.

Each report records priorities, confidence, file/line evidence, causal paths,
proposed fixes, validation suggestions, storage/wire implications and a coverage
ledger including accepted boundary representations. These are source audits;
they do not provide runtime-test proof.

Reports:

- [PR audit](2026-10-31-pr-13428-typing-audit.md)
- [Devel services](2026-10-31-agentplane-typing-services.md)
- [Devel runtime and UI](2026-10-31-agentplane-typing-runtime-ui.md)

The primary user checkout and its uncommitted changes are outside the audit
workspace. Findings are reviewed before implementation is dispatched.
