# Anthropic follow-ups

The central sampler calls organization-paid Anthropic Messages directly, with native compaction,
prompt caching, cache diagnostics, strict tool input, eager argument streaming, thinking binding
validation and cache-preserving effort changes; the [README](../README.md#model-contracts) and
[SPEC](../SPEC.md) describe the contract, and the
[compatibility record](../../../debug/2026-10-30-anthropic-compaction-compatibility.md) holds the
provider evidence. This plan holds the follow-ups. Status and dependencies
belong in the [task DAG](../../../plans/task_dag.md#central_sampler_background_compaction--summarize-while-sampling-continues).

## Dynamic tool definitions

Deferred until a concrete Actions/MCP workflow needs tool additions, removals or schema
changes within a Session. The operator requested keeping this follow-up in the DAG.
Use [inline tool definitions](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages)
with `inline-tools-2026-09-15` where supported, preserving the original `tools` prefix.
First distinguish provider-visible tool definitions from nested code-mode tools:
changing tools inside the shared host also needs a runtime catalog/description update,
and the provider beta alone does not implement that contract. Coordinate with the
existing Actions-as-tools plan rather than creating a second registry.

Exit when selected: changing tool availability is reflected in subsequent model input
and the runtime catalog, restart/replay preserves the change, and the existing cached
prefix is retained where supported. A removed or unauthorized tool still fails at the
execution authority; provider-visible removal is not credential revocation. Include
thinking/compaction compatibility for the chosen model.

## Background compaction

Follow-up to the implemented on-demand compaction, with no assigned worker or priority.
[Anthropic's background-compaction recipe](https://platform.claude.com/docs/en/build-with-claude/compaction-background)
uses the same summarization request concurrently with ordinary sampling; the
application owns the concurrency and later swap.

Persist a snapshot identity and exact summarized prefix boundary when starting the
request. Continue appending ordinary turns against the original context. At the next
provider-request boundary after a valid result arrives, atomically replace exactly
that prefix with the signed block and retain every appended message in order. Allow
only one pending compaction per Session. Fence result application by ownership and
context generation; an interrupt, reset, model/config change or another context rewrite
must not let a stale result overwrite current context. Failed or uncertain background
compaction preserves the full context and does not by itself invalidate a concurrent
ordinary turn. Define restart reconciliation without replaying uncertain provider or
executor effects. The current single-pending-intent representation needs a distinct
background operation; do not weaken its foreground-effect fencing.

Start with headroom for turns generated while compaction runs, and include both
concurrent requests in concurrency, rate and spend limits. Background compaction must not
overwrite the foreground branch's cache-diagnostics reference. Preserve Anthropic's
[kept-thinking conditions](https://platform.claude.com/docs/en/build-with-claude/compaction-thinking-blocks):
unchanged kept messages and compatible model, system and tools. Verify actual sent
input after the swap; a retained display history does not prove model-context fidelity.

Exit: delayed-response fixtures prove continued tool-using turns, exact suffix
preservation, one-pending behavior, stale-result rejection, restart reconciliation and
failure isolation. A bounded direct real-model case summarizes a prefix while another
turn completes, then continues from the signed block plus retained suffix. Where the model
checks thinking binding, run it with mismatch behavior `error` and a genuine thinking block in
the concurrently appended suffix, and require no dropped thinking.

## Optional Langfuse export

Deferred until trace export is selected for an actual consumer. Export from committed
sampler exchanges with stable Session/turn/exchange IDs; the database remains the
source of request/response content and continuation. Langfuse is an asynchronous sink:
its unavailability does not block sampling or erase canonical evidence. Determine
whether the existing OTel export path suffices before adding another delivery queue.
Export authorization and content retention follow the provider archive's policy.
