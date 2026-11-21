# Stable provider prefixes for sampler conversations

**Status:** design for operator review; no implementation is authorized by this plan.

The goal is that a sampler rollout cannot invalidate reasoning already sent for a Thread. The
operator has accepted that Threads already broken by #15591 stay broken; this design adds no repair
or migration for them. Unsupported prompt or tool differences stay pinned until a new provider
conversation.

## Current requests and stored state

A conversation is pinned to one provider API. On every turn, the sampler renders a full request
from its current route configuration and durable conversation state; it does not use Anthropic
Message Threads, OpenAI `previous_response_id`, or the Conversations API.

- **OpenAI Responses:** `prepareProviderRequest` builds `instructions` from shared
  `protocol.BaseInstructions` plus the conversation's stored instructions. It builds tools from the
  current `toolcontract.ModelSurface`: standard routes send `tools`; Lite routes send an initial
  `additional_tools` item. Input is projected from stored native Responses items, including opaque
  reasoning, calls, and results. Requests use `store: false` and a Session-scoped prompt-cache key.
- **Anthropic Messages:** top-level `system` combines `BaseInstructions` and stored conversation
  instructions; `tools` comes from the current tool surface; `messages` are projected from stored
  native Messages records, including opaque signed thinking and other provider blocks.
- **Durability:** each call has a `sampler_model_request` row with its profile, context, and ordered
  prefix/suffix references. `sampler_content` stores deduplicated profiles, instructions, tool
  definitions, and native records; `sampler_context` retains the ordered conversation chain. Retries
  reconstruct the request from this Agentplane-owned state.

Current `devel` already restores the prior request's system/instructions and tools, plus Lite's
initial `additional_tools`, and appends history. This implicitly freezes those fields for an existing
conversation; changed definitions reach new conversations. Other route fields are still rebuilt
from current configuration, so the baseline is incomplete and implicit.

Staging selects both routes: Anthropic API routes use Messages with adaptive thinking,
`prefix_mismatch_behavior: "error"`, 5-minute prompt caching, cache diagnostics, per-message effort,
and compaction; OpenAI routes use Responses through LLM Ingress, with Lite for official OpenAI routes
and standard mode for other Responses routes. The Anthropic adapter currently sends
`thinking-binding-controls-2026-08-01`, `mid-conversation-output-config-2026-07-01`, and
`compact-2026-09-04`. It does not send either tool-change beta described below.

## Provider mechanisms

**Anthropic.**

- [Preserved thinking](https://platform.claude.com/docs/en/build-with-claude/preserved-thinking.md): a signature binds top-level `system`, the name-sorted set of `tools`, and every earlier message (from the latest compaction block after compaction). Appending history or `role: "system"` messages is safe, as are changes to request parameters outside those fields (`max_tokens`, `output_config`, `tool_choice`, and `metadata`), moving `cache_control`, server-side compaction, and context editing. Adding an unreferenced `defer_loading: true` tool is safe. Beta `thinking-binding-controls-2026-08-01` accepts `error` (reject a mismatch) or `drop_block` (drop thinking); sending `block_binding` without its beta header is a 400. Model changes also drop blocks. Every thinking-model response has `input_transformations` (never null): `[]` when nothing was dropped, otherwise `thinking_dropped` entries with `prefix_binding_mismatch`, `model_binding_mismatch`, or `organization_binding_mismatch`, or `thinking_mismatch_allowed` on unenforced requests; ignore unknown types. Compaction is not documented to re-root `system`/`tools`. Keep signed blocks and the transcript locally; appends use context tokens and compaction only reduces active provider context.
- [Mid-conversation system messages](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages) are GA. Append `{"role":"system"}` to change instructions while retaining system-prompt authority. Persist the message; it costs input tokens and does not replace the baseline.
- [Tool changes](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview#mid-conversation-tool-changes): beta `mid-conversation-tool-changes-2026-07-01` appends `tool_addition`/`tool_removal` `tool_reference` blocks in a system message. A referenced addition must already be in initial `tools[]` with `defer_loading: true`; removal is immediately before an assistant message or last in `messages`. Beta `inline-tools-2026-09-15` appends a system message containing `{"type":"tool_addition","tool":{"type":"tool_definition","definition":{...}}}` to replace an earlier same-name definition while initial `tools[]` stays unchanged. The reference beta is available on Claude API, Bedrock, and Vertex; inline definitions are available on Claude API, Claude Platform on AWS, and Anthropic-managed Google Cloud, not Bedrock, Vertex, or Foundry. Persist updates; they use context and require runtime support.
- Compaction/context editing preserve the binding but do not re-root `system`/`tools`; staging uses beta `compact-2026-09-04`. [Message Threads](https://platform.claude.com/docs/en/build-with-claude/message-threads) use beta `message-threads-2026-08-12`. Threads send `system`/`tools` only at creation; model, `tool_choice`, thinking, context management, output config, and beta set are fixed (otherwise `thread_fingerprint_mismatch`). Changing system/tools requires a new Thread and loses continuation of signed thinking. Threads expire 5 minutes after the last request, are deleted within an hour, and do not support compaction. They optimize transport/latency; keep Agentplane's transcript and replay on `thread_not_found`.

**OpenAI Responses.**

- [Preserved reasoning](https://developers.openai.com/api/docs/guides/preserved-reasoning) and [reasoning](https://developers.openai.com/api/docs/guides/reasoning): a reasoning item's prefix is earlier conversation history. Changing `tools`, `instructions`, `text.format`, or earlier input silently drops following reasoning items; model-family changes also lose reasoning. `input_transformations` identifies removed items with `prefix_mismatch`; append-only history is safe. The guides name no beta header. Its complete transformation shape and opt-in status are undocumented. Keep reasoning and request history locally; no setting makes a mismatch fail.
- [Tool forms](https://developers.openai.com/api/docs/guides/tools-tool-search): append a developer message for new instructions; an `additional_tools` developer input item with `tools` adds tools from that point. Same-name definition replacement is undocumented, so its description stays pinned. `tool_choice.allowed_tools` narrows callable tools but is not an authorization boundary. `configuration_update` before the next user message changes reasoning effort and `text.format` only on GPT-6+ single-agent routes. These documented forms name no beta header; store appended items locally, which consume input/context.
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) reuses matching request prefixes to reduce repeated-input cost/latency. It does not make a changed prefix valid or preserve reasoning, and removes no Agentplane state.
- [Server-side state](https://developers.openai.com/api/docs/guides/conversation-state): `previous_response_id` and the Conversations API continue provider-held state but do not bypass the prefix rule. Responses are stored for 30 days by default; Conversation items have no TTL. `store: false` is stateless and uses `encrypted_content` reasoning. Provider state may reduce history resends, but adds provider retention/lifecycle and does not replace Agentplane's transcript, effects, request journal, or recovery evidence.

## Designs

1. **Freeze each conversation's first rendered request.** This is portable and extends current behavior: all prompt/tool changes affect only new provider conversations. Reuse the first `sampler_model_request.profile_id`; no new table is required. The cost is stale instructions and tools in long-lived conversations.
2. **Freeze a baseline and append provider-native changes.** This is the recommendation. Add a small `sampler_conversation_prompt` row with the baseline profile/hash and applied surface revision/hash; reference existing `sampler_content` instead of duplicating bodies. At a settled turn boundary, compare the current surface and persist the exact update with the request before dispatch. Anthropic appends instruction changes as `role: "system"`; changed same-name tool definitions as an `inline-tools-2026-09-15` `tool_definition`; and additions/removals as `tool_reference` blocks under `mid-conversation-tool-changes-2026-07-01` (with new tools predeclared using `defer_loading`). OpenAI appends developer instructions and new tools through `additional_tools`. A changed OpenAI tool definition, or any other difference without a documented append-only form, stays pinned until a new provider conversation. These betas and per-route update handling cost implementation work, context tokens, and provider compatibility.

Provider-held state is **not** a third design: Anthropic Message Threads and OpenAI Responses/Conversations are optional transport optimizations after the baseline-and-update rule. They do not make changes to bound fields safe and do not remove Agentplane's canonical state or recovery journal.

## Recommendation and acceptance

Implement design 2 with design 1 as its fallback. Keep the initial rendered profile immutable; compare the current surface at a settled turn boundary; append only a provider-documented update; otherwise defer the difference until a new provider conversation. Compaction is not a reset boundary. Never edit earlier messages, change a bound top-level field, use `drop_block`, or make provider-held state the only copy. Persist the update item and surface revision atomically with the prepared request so retries and restarts replay the same bytes.

Add Store/request coverage for baseline immutability and retry/restart replay. Add opt-in live continuation tests for both APIs: start with an `exec` tool description v1 and retained reasoning, then change the source tool surface to v2. Anthropic sends v2 through the inline-tools beta with `prefix_mismatch_behavior: "error"`; assert the previous signed thinking is unchanged and `input_transformations == []`. OpenAI keeps sending v1 because no same-name replacement form is documented; assert reasoning remains and no `prefix_mismatch` transformation appears (assert an empty array only if the field is enabled and returned). Separately verify an appended developer instruction or new tool preserves reasoning. Restart between turns and verify exact update replay. For the unsupported OpenAI description edit, assert it stays deferred and a new provider conversation starts with v2.
