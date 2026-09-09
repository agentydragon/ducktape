# Driver-provided tools and background work on the seam

Status: **deferred pending a real consumer.** Provider behavior is settled in
[`../docs/driver_tools.md`](../docs/driver_tools.md) and
[`../docs/background_work.md`](../docs/background_work.md). Any future runner surface must reuse the
implemented [Action Service contracts](../action_service/SPEC.md), not invent a second
incompatible tool-request lifecycle.

## Input-delivery research prerequisite

Future input-delivery work must follow `INPUT_DELIVERY` in [the DAG](task_dag.md): re-read landed
Claude/Codex queueing evidence, compare native receipt/promotion/coalescing semantics, and refresh
captures before revising the common protocol. A harness-local queue is neither a background Action
Execution nor proof that both providers support the same per-input acknowledgement or cancellation.

## Driver-provided declarations

A driver may eventually declare model-visible tools, but that declaration is not an ActionRequest
and does not grant execution authority. If a declared tool invokes work outside the Sandbox:

- its stable Action identity and parameter/result contract come from the canonical Action catalog;
- invocation creates the same invariant ActionRequest used by other callers;
- Decision and at-most-one Execution remain separate;
- reviewed Action Service configuration selects the Executor; and
- pending/result delivery uses canonical receipts and durable events.

Do not build an MCP registry, parallel `ToolRequest`, or harness-specific approval object. The
provider-specific declaration adapter may still need to decide whether its tool set is immutable per
session or replaceable; Codex's fixed thread schemas and Claude's tool-list behavior remain visible
constraints rather than reasons to fork the Action contract.

## Background work

Background work exposed by a harness is local runtime state, not automatically an Action Execution.
The smallest common provider floor remains list + stop by harness ID, correlated to the originating
native tool call. Expose it only when a product consumer needs to observe or stop harness-local work.

If background work invokes an external Action, the durable external effect is still represented by
its ActionRequest/Decision/Execution. A harness task ID may be correlation evidence; it must not
become a second retry, idempotency, or authorization authority.

## Acceptance before either surface lands

- one named consumer and user-visible behavior;
- scripted tests against both pinned harness binaries;
- a mapping showing which fields are provider declaration/runtime correlation and which belong to
  Action definition/request/execution;
- no duplicate Decision, retry, credential, or result-delivery semantics; and
- an explicit unsupported result where one provider cannot honor a mutation such as replacing tools
  mid-thread.
