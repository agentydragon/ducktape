# Anthropic Messages and compaction

Planning request, 2026-10-30: support organization-paid Anthropic API models in the
central sampler, including compaction and real-model acceptance. The operator accepted
direct organization API access and the named-connection configuration sketch on
2026-10-30: the sampler calls Anthropic and stores provider exchanges in its own
database. Langfuse export can follow independently. The implementation adds named connections, native Messages, signed compaction and request features.
The chart wires the existing organization credential supplier; usage is billed to that organization
directly, outside LiteLLM virtual-key budgets. No deployment has changed.

Status and dependencies belong in the [task DAG](../../../plans/task_dag.md#central_sampler_anthropic_routing--direct-provider-connections-and-credentials).

## Provider route and credentials

The selected route sends native Messages to `https://api.anthropic.com`. Agentplane
owns model admission, request/response persistence and usage records. Native
compaction avoids LiteLLM's current request filtering gap.

This selection is limited to the central Anthropic adapter. Existing Responses
and wrapped-harness paths keep their own configured connections. Define provider
connections and their model bindings in server-owned sampler settings: public model
route, connection, provider model ID, API and supported request features. For example,
`anthropic-api/ant-messages/claude-sonnet-5` maps explicitly to `claude-sonnet-5` on the
Anthropic connection; the proxy route string is not an Anthropic model ID. Authentication
and routing are not inferred from the spelling of the model name. Session callers
choose configured models, never endpoints or credential sources.

Use the existing declarative organization credential supplier, with a Secret mounted
only into the sampler and read with rotation support. The chart owns the sampler-only Secret mount and direct egress policy. Organization billing owns
the resulting direct API usage; LiteLLM virtual-key budgets do not apply. Keep
credential values out of exchange records, errors, History and trace exports. Update
the system overview and traffic diagrams when the direct edge is implemented.

The existing BuildBuddy `OPENAI_API_KEY` secret is a LiteLLM virtual key. It cannot
authenticate directly to Anthropic. The operator authorized reusing the existing organization API key for bounded direct acceptance
through per-invocation redacted `secret-env-overrides`; this does not provision a persistent
BuildBuddy secret. [Provision a dedicated `ANTHROPIC_API_KEY` later](https://github.com/agentydragon/ducktape/issues/13063),
with explicit spend limits and model selection. Retaining a
LiteLLM-based test separately does not prove the direct production path.

## Provider exchanges in the sampler database

Reuse and extend the sampler's existing intent/evidence storage. Its current
Responses loop already records requests, streamed provider events and terminal
results; this is an extension to that owner, not a new app-owned store.

Persist the final serialized request body before dispatch. Give each attempt an
exchange ID linked to Session, turn, ownership generation and sampling/compaction
purpose. Store the provider model ID, API/version/beta selection, provider request ID,
HTTP status, ordered response content, terminal outcome and usage. Preserve unknown
provider fields, signed thinking/compaction blocks, error bodies and the observed
prefix of interrupted streams. Retain raw body/event payloads alongside any typed
projection; a reconstructed final message is not a substitute for the received stream.

Authentication headers are not part of this content capture. A dispatched request
with no observed terminal response remains outcome-unknown; database logging cannot
prove delivery or recover bytes never received. Bound capture buffering and preserve
the sampler's failure/backpressure behavior if database writes fail. Test a database
write failure before dispatch and after a response prefix has arrived.

Compaction replaces active model context only. The full exchange archive and retained
History remain available under their existing authorization. User-facing display
limits do not truncate the canonical provider content. Backend capture and continuation
work with the integration app unavailable.

## Messages and ordinary compaction

Implement native Messages streaming, system instructions, JSON `exec`, tool-result
ordering, notifications, images, provider stop reasons and usage. Keep execution and
effect journaling shared with Responses. Store provider-native continuation rather
than translating signed content through a lossy common transcript.

Use server-side on-demand compaction for both `Compact` and the sampler's automatic
token threshold. Send `compaction: {"type": "summarize"}` with
`compact-2026-09-04`; retain the returned block and signature exactly, and send the beta
on later requests containing it. Compact at a settled tool boundary. Commit context
replacement and command completion together only after a successful compaction result.
Missing summaries, truncation, cancellation and uncertain requests preserve the prior
context and truthful command outcomes. Count compaction usage from `usage.iterations`,
and include cache tokens when estimating context size. Stored-format changes use new
migrations and preserve existing Sessions.

Acceptance uses deterministic fixtures for streaming, failure and transaction
boundaries, plus a bounded real-model run through BuildBuddy on the direct path:
tool execution, compaction, sampler restart, recall of a required task fact, and another
tool operation. Cover explicit and automatic compaction. The organization's Models API
reported compaction support for `claude-opus-5`, `claude-sonnet-5` and `claude-fable-5`
on 2026-10-30; `claude-haiku-4-5-20251001` reported unsupported. Refresh capabilities
when selecting the acceptance model.

## Selected request features

Operator selections, 2026-10-30. These are part of the initial Anthropic implementation;
temporary system instructions and task budgets are explicitly out of current scope.
Expose typed per-model settings and derive the required headers from enabled features
and retained continuation. Validate feature/model/thinking-mode combinations before
dispatch; do not silently change the selected contract after a provider rejection.

### Prompt caching and diagnostics

Enable [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
with a configured TTL and automatic cache control as the starting policy. Keep the
system prompt, tools, schema serialization and earlier messages stable across turns;
append changing environment context rather than rewriting the cached prefix. Store
cache creation/read usage and include both in context-budget calculations.

Opt into [cache diagnostics](https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics)
with `diagnostics.previous_message_id`: `null` for the initial request and the prior
response ID for later requests on the same conversation branch. Persist the response
diagnostics, including the `message_start` field on streaming requests. Comparison
results describe prompt divergence; usage establishes an actual cache read. Missing,
pending or unavailable diagnostics are not evidence of either a hit or a miss.
Background compaction must not overwrite the foreground branch's diagnostic reference.

Require a direct real-model BuildBuddy acceptance test against Haiku, preferably
`claude-haiku-5-5` so the same test can exercise per-message effort. Use a unique,
per-run prefix that remains unchanged between requests and exceeds the selected
model's minimum cacheable size. Complete the first response, assert positive
`cache_creation_input_tokens`, then append a turn and assert positive
`cache_read_input_tokens` covering the warmed prefix. Keep the run bounded and complete
within the configured TTL. Assert provider usage recorded in the sampler DB, not
latency or the mere presence of `cache_control`. No blanket retry of a failed turn.
If Haiku 4.5 is selected for a separate caching test, size its prefix for that model's
larger minimum; it cannot cover the per-message-effort case.

### Cache-preserving effort changes

On [supported models](https://platform.claude.com/docs/en/build-with-claude/effort#per-message-effort-beta),
translate the existing effort-change command into a persisted per-message
`output_config.effort` using `mid-conversation-output-config-2026-07-01`. Keep the
top-level effort, system prompt and tools stable. Honor the API's new-user-input
boundary: a user message containing only tool results does not activate a changed
effort. Use adaptive thinking where the selected model requires it for effort changes.

Extend the real Haiku 5.5 cache test with another turn at a different allowed effort;
assert the appended control and stable prefix in the stored wire request, plus
continued positive cache reads. Deterministic tests cover admission, ordering,
restart, unsupported model/mode rejection and activation after new user input.

### Thinking-block validation

Support explicit `thinking.block_binding.prefix_mismatch_behavior` with
`thinking-binding-controls-2026-08-01` on models that implement the prefix check.
Preserve signed blocks exactly and persist `input_transformations` when the API
reports dropped content. A successful request that dropped thinking does not prove
thinking continuity.

Use the [provider's validation recipe](https://platform.claude.com/docs/en/build-with-claude/compaction-thinking-blocks)
for bounded live acceptance: keep at least one genuine thinking block after the
summarized prefix, set mismatch behavior to `error`, and continue on a model that
actually checks binding. Require successful continuation with no dropped thinking.
Deterministic negative fixtures cover altered prefixes and signatures. Background
compaction adds its concurrent-suffix case later; the foreground case does not wait
for that follow-up.

### Strict tool input and eager streaming

Use [`strict: true`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)
for the JSON `exec` definition after adapting its schema to Anthropic's supported
subset. Preserve the runtime's exec-versus-wait validation and operation limits.
Schema constraints do not validate the JavaScript program or authorize its effects.

Enable [`eager_input_streaming`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming)
on `exec` for early code visibility and retain the streamed argument fragments. Use
the per-tool field, not the legacy beta header. Dispatch only after complete arguments
and the enclosing provider result have been validated under the sampler's existing
effect rules. Cover fragmented JSON, truncation, cancellation and malformed input;
incomplete code must never execute. Exercise strict/eager compatibility with the
actual generated schema in a live tool turn.

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
thinking/compaction compatibility for the chosen model. This does not block the initial
fixed-tool Anthropic adapter.

## Background compaction

Follow-up after ordinary on-demand compaction, with no assigned worker or priority.
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
concurrent requests in concurrency, rate and spend limits. Preserve Anthropic's
[kept-thinking conditions](https://platform.claude.com/docs/en/build-with-claude/compaction-thinking-blocks):
unchanged kept messages and compatible model, system and tools. Verify actual sent
input after the swap; a retained display history does not prove model-context fidelity.

Exit: delayed-response fixtures prove continued tool-using turns, exact suffix
preservation, one-pending behavior, stale-result rejection, restart reconciliation and
failure isolation. A bounded direct real-model case summarizes a prefix while another
turn completes, then continues from the signed block plus retained suffix. Confirm
model-specific thinking behavior when that model supports the relevant check.

## Optional Langfuse export

Deferred until trace export is selected for an actual consumer. Export from committed
sampler exchanges with stable Session/turn/exchange IDs; the database remains the
source of request/response content and continuation. Langfuse is an asynchronous sink:
its unavailability does not block sampling or erase canonical evidence. Determine
whether the existing OTel export path suffices before adding another delivery queue.
Export authorization and content retention follow the provider archive's policy.
