# Routing options: requirements and source audit

Research date: **2026-10-10**. Part of [#9574](https://github.com/agentydragon/ducktape/issues/9574),
following the [routing-first review](routing_review.md). **Source/documentation research,
not a live compatibility certification or a replacement decision.** No inference, model
loading, credential changes, deployment changes or catalogue suppression performed.

## Current operator requirements

These refinements supersede the broader proposed requirements in the initial review:

- Clients: Agentplane, Codex CLI, Claude CLI, OpenClaw; OpenCode is nice to have.
  We can change Agentplane and repo-owned scripts' API shapes. Haku recall/search
  embeddings are out of scope because they are expected to be deleted separately.
- Priority upstreams: Codex subscription, Claude subscription, Ollama. Embeddings,
  Tana and Gemini are nice to have. Direct paid Anthropic, Mistral, Groq, Whisper and
  Antigravity are not needed near/medium term. This does not authorize deleting them.
- Required: tools and core protocol correctness, structured output/JSON schemas,
  usable route context rather than low fallback windows, and full request-context
  plus sampled-response logging. Image input is unnecessary. Schema support means
  more than a prompt instruction to return JSON; tool schemas and output schemas
  need separate coverage.
- Logging: Langfuse is acceptable, alternatives are acceptable; either client-facing
  or upstream content is sufficient. No need to capture both. Exclude credentials;
  decide retention/access for sensitive full-content logs before rollout.
  **Operator clarification: do not silently drop traces or their request/response
  content because they are large.** This includes the exporter, transport, collector
  and storage path, not merely the gateway's local capture. A surviving metadata-only
  span does not satisfy full-content logging.
- Auth/credential isolation is preferred, not a hard product requirement. Spending
  limits/cost accounting and automatic model fallback are not requirements. Existing
  security boundaries still apply during research; removals require a reviewed migration.
- Some cross-protocol translation is wanted but secondary. Multiple proxies are
  acceptable if useful. Neither every client/upstream pairing nor one universal
  protocol is required. Keep unsupported semantics explicit rather than silently lossy.

## Evidence and version scope

Inspected repository snapshots below, plus Ducktape's existing pinned-version audits.
Source presence is stronger than marketing, but still does not prove a working account,
complete stream round trip or effective full-context logging. Default-branch snapshots
are **not proposed deployment pins**. Do not replace the deployed CLIProxyAPI/Ollama or
client versions merely because newer source was inspected.

| Component                               | Inspected revision / qualification                                                                                                                                                                                                                           |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| CLIProxyAPI                             | [`3de4e248f6bb`](https://github.com/router-for-me/CLIProxyAPI/tree/3de4e248f6bb12fc7dfafb183404f3b248578922); deployed pin remains `7fac6b15bcfe`, separately audited                                                                                        |
| Ollama                                  | [`v0.34.4`](https://github.com/ollama/ollama/tree/v0.34.4) routes/Responses/Anthropic code (matches previously observed installed version); current docs at [`eab97e9f92b9`](https://github.com/ollama/ollama/tree/eab97e9f92b9a25c2d52d2cc6c1b1c99bd9fae21) |
| Bifrost                                 | [`909f4304eeb2`](https://github.com/maximhq/bifrost/tree/909f4304eeb2066fc850201289a7071394aee5b1), default `dev` branch; distinguish OSS and enterprise releases/features                                                                                   |
| Agent Router, formerly Envoy AI Gateway | [`f4571d6bd8f5`](https://github.com/theagentrouter/agent-router/tree/f4571d6bd8f53696c4e9105f365b556db7b249a8); repository redirect and rename verified; release v1.2.0 published 2026-10-07                                                                 |
| Portkey Gateway                         | [`669825cbe89e`](https://github.com/Portkey-AI/gateway/tree/669825cbe89ee51569918b8f78a9db486fd69dd4), `main`; README advertises a separate 2.0 prerelease, not audited here                                                                                 |
| TensorZero                              | [`62eb8f63e8ec`](https://github.com/tensorzero/tensorzero/tree/62eb8f63e8ec62018d70420dbf1a8c5d1c026315); GitHub repository metadata reported **archived=true** on research date                                                                             |
| Codex                                   | [`1badea29abf4`](https://github.com/openai/codex/tree/1badea29abf49e56530a778ac3bb2d3da3bc4d5e); also existing runner audit of 0.157.0                                                                                                                       |
| OpenCode                                | [`7b3d4ce3a7db`](https://github.com/anomalyco/opencode/tree/7b3d4ce3a7dbd2a6d3637722a0d5f22a7d086937); current source, not an installed-client observation                                                                                                   |
| OpenClaw                                | [`171df4e1c5a0`](https://github.com/openclaw/openclaw/tree/171df4e1c5a08e1da631c4fff50c420db2bb6f84); current source/docs, not the previously audited 2026.9.5 deployment                                                                                    |

## Client-side constraints: replacing LiteLLM does not solve these

| Client     | Wire/configuration evidence                                                                                                              | Context consequence                                                                                                                                                                                                     |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agentplane | Runner launches native Codex/Claude adapters; ingress model-config is our client policy, not LiteLLM discovery                           | We control the supplied budget, but the harness can still clamp/interpret it                                                                                                                                            |
| Codex CLI  | Current config schema's `WireApi` has **Responses only**. Custom provider/base URL is supported                                          | `model_context_window` is clamped to `max_context_window`; unknown-model fallback has 272000 for both, 95% effective. Merely setting a larger number or changing gateway discovery does not fix it                      |
| Claude CLI | Repo's 2.1.252 adapter uses Messages and sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS`; gateway base URL/model overrides already exist           | Verify effective context/compaction and recognition on the chosen route, not just environment injection. Claude is not an open-source wire implementation audited here                                                  |
| OpenCode   | Provider loader includes Anthropic-compatible, OpenAI Chat and Responses-compatible adapters; config exposes context/input/output limits | Select the right adapter and explicit route limits; do not assume catalogue defaults describe subscription accounts                                                                                                     |
| OpenClaw   | Custom providers support different API adapters, including Messages and OpenAI-compatible paths; native Ollama integration exists        | Explicit `contextWindow` matters. Current docs say missing metadata eventually falls back to 200000. Current route policy can also choose different harness runtimes; compare deployed version before applying settings |

Sources: [Codex config schema][codex-config], [Codex clamp/fallback][codex-model],
[OpenCode provider loader][opencode-provider], [OpenCode limit schema][opencode-config],
[OpenClaw custom providers][openclaw-custom], [OpenClaw runtime selection][openclaw-runtime],
[existing client audit](client_budgets.md).

Codex also exposes startup-only `model_catalog_json`; per-thread config does not reload
it. The [pinned 0.157.0 audit](client_budgets.md#codex-local-model-catalogue-2026-10-10)
confirms per-model context windows, override ceilings, effective input headroom and
compaction thresholds in full model entries, not a sparse limits map. This could
simplify Agentplane's runner independently of the proxy/logging choice, but is not a
tested integration or permission to copy every backend claim into a harness.
Coordinated native model aliases are another option, but must not select the wrong
capabilities or merely inherit another too-small maximum.

## Options matrix

“Implemented” below means a source path was found; it is not live certification.
Subscription lifecycle means OAuth/account refresh and subscription-specific request
handling, not merely a configurable OpenAI/Anthropic API base URL.

| Option                                                                          | Protocols and translation                                                                                                                                                                       | Subscriptions / Ollama / embeddings                                                                                                                                                                                                                           | Full-content logging                                                                                                                                                                    | Fit for this scope                                                                                                                                                            |
| ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Keep CLIProxyAPI + direct Ollama, add/reuse a thin logging/routing boundary** | CLIProxyAPI implements Messages, Responses and Chat, including translators. Ollama itself exposes Responses, Messages and Chat, with important gaps below                                       | Reuses current subscription lifecycle. Ollama has native/OpenAI embeddings. No embeddings route in the inspected CLIProxyAPI v1 route block; keep embeddings separate                                                                                         | CLIProxyAPI has opt-in file request/response logs; Ollama/Agentplane capture can be investigated. A reliable shared Langfuse exporter and retention still need engineering/verification | **Best minimal-topology hypothesis to test**, not a ready replacement. Eliminates unnecessary generic translation without pretending subscription gateway behavior disappears |
| **Bifrost**, optionally only for compatibility/logging                          | Explicit Codex/Claude CLI integrations, Responses and Anthropic SDK surfaces, cross-provider translation, and separate raw passthrough routes                                                   | Custom provider bases can front CLIProxyAPI. Native subscription refresh parity not established; docs distinguish Claude OAuth token forwarding from provider-key passthrough. Ollama Chat/embeddings implemented; its Responses path translates through Chat | Built-in content log store and OTLP/Langfuse docs. **Raw OTLP bodies over 256 KB are dropped**; normalized content and storage/export limits need testing                               | **Strongest packaged compatibility/logging candidate**, but not an escape from model-driven defaults; see clamping below                                                      |
| **Agent Router / Envoy AI Gateway**                                             | Documents native Messages, Responses, Chat and embeddings. Responses translator inspected is OpenAI-to-OpenAI, not a universal Responses-to-Claude bridge                                       | Standard backends/custom routing can retain CLIProxyAPI; no inspected subscription login/refresh replacement. Ollama via compatible endpoints                                                                                                                 | OpenInference request/response tracing and OTLP. GenAI tracing mode currently maps content for Chat/Messages, **not Responses**                                                         | Plausible Kubernetes-native boundary if existing Envoy integration is valuable; more control-plane work than a thin proxy. End-to-end log completeness still unproved         |
| **Portkey Gateway main / 1.x line**                                             | HTTP routes exist for Messages, Responses, Chat and embeddings. Anthropic provider config has Messages/Chat transforms but no Responses operation                                               | OpenAI-compatible backend/Anthropic/Ollama integrations; subscription lifecycle not established                                                                                                                                                               | Local gateway log UI documented; OSS persistence/full-content/Langfuse behavior not established by this audit. Do not import hosted/enterprise claims into OSS                          | Lower priority: provider-specific gaps and 1.x/2.0 split need another audit before a fit claim                                                                                |
| **TensorZero**                                                                  | Inspected OpenAI-compatible server routes expose Chat and embeddings, **not native Responses or Messages ingress**. “Call Responses” docs describe an upstream provider behind its own/Chat API | Compatible providers incl. Ollama; would still need subscription gateway                                                                                                                                                                                      | Own observability/database/UI is a substantial part of the product                                                                                                                      | Poor fit for unmodified modern Codex/Claude requirements; archived repo status is another reason not to prioritize                                                            |
| **Retain LiteLLM selectively**                                                  | Existing wide compatibility, already integrated                                                                                                                                                 | Existing subscriptions through CLIProxyAPI and Ollama                                                                                                                                                                                                         | Existing Langfuse callback                                                                                                                                                              | Lowest migration effort; retain for a narrow proven path if needed, rather than requiring full feature parity or assuming every catalogue effect can be switched off          |

Sources: [CLIProxyAPI routes][cpa-routes], [file logger][cpa-log], [Bifrost passthrough][bf-pass],
[Bifrost Ollama][bf-ollama], [Bifrost OTLP/Langfuse][bf-otel], [Agent Router endpoints][ar-api],
[Agent Router tracing][ar-trace], [Portkey routes][pk-routes], [Portkey Anthropic config][pk-anthropic],
[TensorZero ingress routes][tz-routes], [TensorZero Responses guide][tz-responses].

## Findings that change the decision

### 1. Native Ollama is a more capable starting point than an extra translator

The pinned [Ollama routes][ollama-routes] already expose `/v1/responses`, `/v1/messages`,
`/v1/chat/completions`, `/v1/embeddings` and `/v1/responses/compact`. The inspected
[Responses conversion][ollama-responses] maps `text.format.schema` into the internal
`Format` field. This provides a concrete schema-preserving path to examine, not just
marketing about “OpenAI compatibility.” Model/schema enforcement still needs testing.

But **native compatibility is not complete**. Current docs describe stateless Responses
(no `previous_response_id`/`conversation`) and limitations around tool-choice controls.
The pinned [Anthropic request type][ollama-anthropic] defines `output_config` with only
`effort`, not `format`; this path does not establish support for Anthropic output JSON
schemas. Therefore direct Claude-to-Ollama is **not accepted as satisfying all our
requirements**, despite working text/tool demos. Decide whether to fix/bridge that
specific gap or keep this optional client/upstream combination out of the initial set.
The [current compatibility docs][ollama-docs] also list missing count-tokens, prompt
caching and other Messages features. Explicitly check which the real harness needs.

Ollama's allocated serving context is a separate configuration. Preserve/review model
tags and `num_ctx`; removing LiteLLM does not automatically provision larger windows.

### 2. CLIProxyAPI already provides translation, but one schema path is prompt-only

Current [Responses-to-Claude conversion][cpa-to-claude] reads `text.format` (or
`response_format`) and appends `BuildClaudeStructuredOutputInstruction(...)` to the
system prompt. **That is not native schema enforcement.** Do not label this translated
path conformant merely because it produces plausible JSON. It also supplies explicit
32000/default-family output budgets when absent; translation is not parameter-neutral.

The reverse [Messages-to-Codex conversion][cpa-to-codex] explicitly maps
`output_config.format` into `text.format` with a JSON schema. This direction has a
stronger implementation basis, but account enforcement/strictness still needs evidence.
Native Messages-to-Claude and Responses-to-Codex should be evaluated separately from
these cross-protocol paths.

The [Codex Responses adapter][cpa-codex] still deletes `max_output_tokens`,
`max_completion_tokens`, `temperature` and `top_p`. Cutting out LiteLLM will not make
those parameters effective or make this subscription endpoint a drop-in paid OpenAI API.

### 3. Bifrost is not free of model-derived request policy

The [Anthropic Responses translator][bf-anthropic] initializes the output cap through
`GetMaxOutputTokensOrDefault` and clamps a supplied cap with
`clampToModelOutputCeiling`. It uses resolved model capabilities to alter thinking,
sampling and tool behavior. Some backend variants implement structured output through
synthetic tools rather than native output formats. Those are real compatibility choices,
not evidence that this gateway preserves all caller semantics automatically.

Its [catalogue resolver][bf-resolver] can infer a provider and populate fallback providers
for unprefixed names. Explicit provider selection and passthrough skip this resolver.
[Passthrough][bf-pass] forwards native path/body through a separate execution path,
while still selecting credentials/plugins; test that exact path rather than generalizing
from translated APIs. No claim that the catalogue is impossible to disable in Bifrost.

Its [Codex guide][bf-codex] itself notes that Codex's picker uses Codex's catalogue,
not Bifrost's. So the client context/recognition problem remains even with this candidate.

### 4. “Has Langfuse integration” is weaker than full long-context logging

**Operator clarification: logging is required for every API format we end up using.**
An exporter that handles Chat Completions but loses Responses or Messages content
fails this requirement. Selecting multiple proxies does not exempt any served path.
Upstream or downstream capture remains acceptable; a translated path need not log
both representations, but its chosen capture must be complete and associated with
that request, not merely summarize tokens or the final assistant text.

Use a per-protocol logging acceptance matrix for OpenAI Responses, OpenAI Chat
Completions, Anthropic Messages, and native Ollama/embedding APIs if retained. For
each applicable streaming/non-streaming form, verify complete request context,
system/instruction fields, tool definitions/calls/results, output-schema parameters,
sampled content and returned reasoning where exposed. Include terminal status,
upstream errors, partial streams and cancellation; incomplete output must be marked
incomplete, never presented as a successfully captured full response. Require the
same large-payload/no-silent-loss behavior for every selected format. This does not
require adding protocols or unpausing routes just to populate the matrix.

Bifrost documents full normalized content export and raw payload export to OTLP/Langfuse,
but raw bodies **over 256 KB are dropped rather than truncated**. Our successful Luna
probe's request alone was about 2 MB. This does not prove normalized export is incomplete,
but makes “enable raw logging” insufficient. Verify normalized messages/tools and the
whole collector/storage path using large synthetic payloads without inference.

**Acceptance gate:** Bifrost's documented raw-OTLP path does not meet the requirement
as-is. This is body loss, not proof that the whole trace is dropped, but either silent
trace loss or silent selected-content loss is unacceptable. Keep Bifrost conditional
on a verified alternative (complete normalized content, a corrected exporter, or durable
payload storage with reliable trace links and full retrieval). Do not assume another
representation is lossless without comparing captured content. No silent sampling,
truncation or size-triggered omission of the required content.

Exercise maximum intended context sizes, collector record/attribute limits, temporary
exporter failures and backpressure. Durable spooling/retry or referenced payload storage
may help; an unbounded in-memory queue is not a durability strategy. Log-delivery failure
must be observable. The operator has not chosen whether persistent logging failure should
block inference or use another durable sink; decide that failure policy before rollout,
rather than promising impossible unconditional delivery.

Agent Router documents OpenInference full-content defaults, but its alternative GenAI
mode currently omits Responses message content. Choose instrumentation consciously.
CLIProxyAPI file logging spools streaming responses to temporary files; that is useful
capture evidence, not proof of credential sanitization, durable retention or Langfuse export.

Ducktape already has body/chunk logging in `agentplane/llm_ingress/server.go` and a local
`agentplane/capture/llm_recording_proxy.py`. They are candidates for reuse, not a complete
multi-client logging service. Container/collector record-size limits and stream completion
must be checked before claiming those logs meet the requirement.

## Optional upstreams do not need to shape the main data plane

- **Tana:** today `tana/litellm_proxy/custom_handler.py` is a LiteLLM custom provider,
  not proof of an independent drop-in HTTP upstream for every candidate. Keeping a
  small Tana-only LiteLLM service behind a native Messages path is one possible way
  to retain this optional integration without routing every request through LiteLLM.
  Extracting/exposing the provider is another, separate engineering decision. Neither
  is implemented or authorized to reactivate the parked routes here.
- **Gemini:** CLIProxyAPI and the broader candidate gateways have Gemini integrations,
  but distinguish API-key access from any subscription/OAuth path. Select the desired
  account and test schema/tool translation before treating provider-list presence as
  acceptance. Antigravity is intentionally not part of the near-term acceptance matrix.
- **Embeddings:** Ollama's existing `/api/embed` or `/v1/embeddings` can remain a separate
  path. Most general gateways also have embeddings endpoints; do not route them through
  a subscription bridge merely for one-host uniformity. Preserve model/dimension identity
  and full-input/truncation behavior for any retained indexes.

## Suggested next slice: two narrow prototypes, no live migration yet

1. **Native-path baseline:** define explicit Responses-to-Codex-subscription,
   Messages-to-Claude-subscription, and Responses-to-Ollama paths through a full-content
   capture boundary. Keep embeddings separate. Reuse current credential holders.
2. **Compatibility contender:** compare Bifrost passthrough plus only the translations
   actually wanted against that baseline. Agent Router is the next alternative if a
   Kubernetes/Envoy solution is preferred; do not implement all candidates in parallel.
3. **Client-context workstream:** configure a real exposed route in Codex's startup model
   catalogue (or another supported recognition path), OpenClaw/OpenCode model limits and
   Claude launch settings. Verify effective budget/compaction rather than only discovery.
   Preserve intentional reserves; Luna's 900419 accepted input is not an exact ceiling.

First use mock upstreams and recorded synthetic streams: multiple tool calls/results,
JSON schema preservation and rejection, reasoning continuity, late SSE errors, cancellation,
unknown slugs, explicit/omitted caps, no silent model fallback, and complete >2 MB logs.
For schema enforcement, forwarding tests are necessary but not sufficient: a later
narrowly authorized account/model test must check that the backend actually enforces it.
No new inference is authorized by this research; the prior two Luna probes are complete.

Deliver the client/route acceptance matrix and decision before changing production
traffic or resuming roster restructuring. Do not preserve unneeded key/team/cost plumbing
for parity, but do not remove it incidentally while evaluating a different data plane.

[codex-config]: https://github.com/openai/codex/blob/1badea29abf49e56530a778ac3bb2d3da3bc4d5e/codex-rs/core/config.schema.json
[codex-model]: https://github.com/openai/codex/blob/1badea29abf49e56530a778ac3bb2d3da3bc4d5e/codex-rs/models-manager/src/model_info.rs
[opencode-provider]: https://github.com/anomalyco/opencode/blob/7b3d4ce3a7dbd2a6d3637722a0d5f22a7d086937/packages/core/src/provider.ts
[opencode-config]: https://github.com/anomalyco/opencode/blob/7b3d4ce3a7dbd2a6d3637722a0d5f22a7d086937/packages/schema/src/config/provider.ts
[openclaw-custom]: https://github.com/openclaw/openclaw/blob/171df4e1c5a08e1da631c4fff50c420db2bb6f84/docs/concepts/model-providers/custom-providers.md
[openclaw-runtime]: https://github.com/openclaw/openclaw/blob/171df4e1c5a08e1da631c4fff50c420db2bb6f84/docs/providers/openai/runtimes.md
[cpa-routes]: https://github.com/router-for-me/CLIProxyAPI/blob/3de4e248f6bb12fc7dfafb183404f3b248578922/internal/api/server_routes.go
[cpa-log]: https://github.com/router-for-me/CLIProxyAPI/blob/3de4e248f6bb12fc7dfafb183404f3b248578922/internal/logging/request_logger_streaming.go
[cpa-to-claude]: https://github.com/router-for-me/CLIProxyAPI/blob/3de4e248f6bb12fc7dfafb183404f3b248578922/internal/translator/claude/openai/responses/claude_openai-responses_request.go
[cpa-to-codex]: https://github.com/router-for-me/CLIProxyAPI/blob/3de4e248f6bb12fc7dfafb183404f3b248578922/internal/translator/codex/claude/codex_claude_request.go
[cpa-codex]: https://github.com/router-for-me/CLIProxyAPI/blob/3de4e248f6bb12fc7dfafb183404f3b248578922/internal/translator/codex/openai/responses/codex_openai-responses_request.go
[ollama-routes]: https://github.com/ollama/ollama/blob/v0.34.4/server/routes.go
[ollama-responses]: https://github.com/ollama/ollama/blob/v0.34.4/openai/responses.go
[ollama-anthropic]: https://github.com/ollama/ollama/blob/v0.34.4/anthropic/anthropic.go
[ollama-docs]: https://github.com/ollama/ollama/blob/eab97e9f92b9a25c2d52d2cc6c1b1c99bd9fae21/docs/api/anthropic-compatibility.mdx
[bf-pass]: https://github.com/maximhq/bifrost/blob/909f4304eeb2066fc850201289a7071394aee5b1/docs/integrations/passthrough.mdx
[bf-ollama]: https://github.com/maximhq/bifrost/blob/909f4304eeb2066fc850201289a7071394aee5b1/docs/providers/supported-providers/ollama.mdx
[bf-otel]: https://github.com/maximhq/bifrost/blob/909f4304eeb2066fc850201289a7071394aee5b1/docs/features/observability/otel.mdx
[bf-anthropic]: https://github.com/maximhq/bifrost/blob/909f4304eeb2066fc850201289a7071394aee5b1/core/providers/anthropic/responses.go
[bf-resolver]: https://github.com/maximhq/bifrost/blob/909f4304eeb2066fc850201289a7071394aee5b1/plugins/modelcatalogresolver/main.go
[bf-codex]: https://github.com/maximhq/bifrost/blob/909f4304eeb2066fc850201289a7071394aee5b1/docs/cli-agents/codex-cli.mdx
[ar-api]: https://github.com/theagentrouter/agent-router/blob/f4571d6bd8f53696c4e9105f365b556db7b249a8/site/docs/capabilities/llm-integrations/supported-endpoints.md
[ar-trace]: https://github.com/theagentrouter/agent-router/blob/f4571d6bd8f53696c4e9105f365b556db7b249a8/site/docs/capabilities/observability/tracing.md
[pk-routes]: https://github.com/Portkey-AI/gateway/blob/669825cbe89ee51569918b8f78a9db486fd69dd4/src/index.ts
[pk-anthropic]: https://github.com/Portkey-AI/gateway/blob/669825cbe89ee51569918b8f78a9db486fd69dd4/src/providers/anthropic/index.ts
[tz-routes]: https://github.com/tensorzero/tensorzero/blob/62eb8f63e8ec62018d70420dbf1a8c5d1c026315/crates/tensorzero-core/src/endpoints/openai_compatible/mod.rs
[tz-responses]: https://github.com/tensorzero/tensorzero/blob/62eb8f63e8ec62018d70420dbf1a8c5d1c026315/docs/gateway/call-the-openai-responses-api.mdx
