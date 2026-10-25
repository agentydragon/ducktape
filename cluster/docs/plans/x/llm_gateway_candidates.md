# LLM gateway candidates

Candidate documentation, source, release records, and repository metadata were checked on 2026-10-24 UTC. Codex CLI sources, Claude Code gateway examples, Anthropic's current Claude Apps Gateway documentation, and the LiteLLM exporter change and runtime validation were checked on 2026-10-25 UTC at the pinned commit/release or living-doc URL listed below. Scores are documentary, not deployment results. “Yes” means the checked source covers the complete fixed-plan item; “Partial” means it covers only part of the item or needs an integration; “No” is used only where a checked route definition excludes the required surface. “Not documented” means the checked primary sources do not establish the behavior.

The score rows reproduce the Must and Should items from [llm_gateway_replacement.md](llm_gateway_replacement.md). Nice items are intentionally omitted. In particular, this comparison does not require Teams, budgets, semantic caching, guardrails, optional providers, transcription, or browser admin features. Every candidate below has a self-hosted deployment path in its primary sources.

## Version and project status

| Candidate                          | Version assessed                                                                      | License                                                                                   | Maintenance at check                                   |
| ---------------------------------- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Bifrost (Maxim)                    | ent-v2.3.2-base, released 2026-10-21 [B1]                                             | Apache-2.0 repository license [B9]                                                        | Active; repository pushed 2026-10-24 [B10]             |
| Portkey AI Gateway OSS             | v1.15.2, released 2026-01-12 [P1]                                                     | MIT [P5]                                                                                  | Repository pushed 2026-05-25 [P6]                      |
| Helicone AI Gateway                | main at 9649b27, last pushed 2025-11-21 [H1]; no GitHub release found [H5]            | LICENSE says GPL-3.0; README badge says Apache, so the checked sources conflict [H4] [H1] | No repository push after 2025-11-21 [H1]               |
| TensorZero                         | 2026.6.0, released 2026-06-04 [T1]                                                    | Apache-2.0 [T8]                                                                           | Repository was archived after its 2026-06-11 push [T9] |
| Envoy AI Gateway, now Agent Router | v1.2.0, released 2026-10-07 [E1]                                                      | Apache-2.0 [E7]                                                                           | Active; repository pushed 2026-10-23 [E8]              |
| agentgateway                       | v1.6.0, released 2026-10-02 [A1]                                                      | Apache-2.0 [A6]                                                                           | Active; repository pushed 2026-10-24 [A7]              |
| Kong AI Gateway                    | Kong Gateway 3.9.4, released 2026-10-12 [K1]                                          | Apache-2.0 core [K6]                                                                      | Active; repository pushed 2026-10-19 [K7]              |
| LiteLLM, stay on current           | Evaluated at the deployed 1.103.0 [L1]; upstream 1.106.3 was released 2026-10-24 [L6] | MIT outside the separately licensed enterprise directory [L5]                             | Active; repository pushed 2026-10-24 [L7]              |
| Higress, additional candidate      | v2.2.5, released 2026-10-04 [I1]                                                      | Apache-2.0 [I4]                                                                           | Active; repository pushed 2026-10-22 [I5]              |

The LiteLLM row is deliberately scored at the deployment pin in the fixed plan. The newer upstream release is recorded as maintenance context, not treated as an automatic upgrade.

## API and upstream compatibility

| Candidate    | M1 Internal and public endpoints | M2 Required provider/model roster | M3 Required upstream auth modes | M4 ChatGPT Messages and Responses lanes | M5 Evaluation callers and run attribution | M6 Five inbound paths and model discovery |
| ------------ | -------------------------------- | --------------------------------- | ------------------------------- | --------------------------------------- | ----------------------------------------- | ----------------------------------------- |
| Bifrost      | Yes [B5]                         | Yes [B2]                          | Yes [B2]                        | Partial [B7]                            | Partial [B6]                              | Partial [B7]                              |
| Portkey OSS  | Yes [P3]                         | Partial [P3]                      | Partial [P3]                    | Partial [P2]                            | Partial [P4]                              | Yes [P2]                                  |
| Helicone     | Yes [H1]                         | Partial [H1]                      | Partial [H6]                    | Partial [H2]                            | Partial [H3]                              | Partial [H2]                              |
| TensorZero   | Yes [T1]                         | Yes [T2]                          | Yes [T2]                        | No [T10]                                | Partial [T4]                              | No [T10]                                  |
| Agent Router | Yes [E4]                         | Partial [E3]                      | Yes [E3]                        | Not documented                          | Partial [E2]                              | Partial [E2]                              |
| agentgateway | Yes [A5]                         | Yes [A2] [A3] [A4]                | Yes [A2] [A3]                   | Partial [A2]                            | Not documented                            | Partial [A2]                              |
| Kong         | Yes [K1]                         | Partial [K2]                      | Partial [K2]                    | Partial [K2]                            | Not documented                            | Partial [K2]                              |
| LiteLLM      | Yes [L4]                         | Yes [L2]                          | Yes [L2]                        | Yes [L4]                                | Yes [L4]                                  | Yes [L2]                                  |
| Higress      | Yes [I3]                         | Partial [I2]                      | Partial [I2]                    | Partial [I2]                            | Not documented                            | Partial [I2]                              |

M2 is the retained roster from the fixed plan: ChatGPT/Codex through CLIProxyAPI, direct Anthropic and Gemini, Ollama, and Rugged's local OpenAI-compatible route. M3 scores bearer keys for OpenAI-compatible upstreams, provider keys for Anthropic and Gemini, and Ollama's in-cluster no-key route. M6 means POST /v1/responses, POST /v1/chat/completions, POST /v1/messages, POST /v1/embeddings, and GET /v1/models.

## Request behavior and Should items

| Candidate    | M7 Streaming and long requests | M8 Tool calls and reasoning options | M9 Stable aliases | S1 Traced Claude subscription route | S2 Custom route slugs |
| ------------ | ------------------------------ | ----------------------------------- | ----------------- | ----------------------------------- | --------------------- |
| Bifrost      | Yes [B7]                       | Yes [B2]                            | Yes [B12]         | Partial [B2]                        | Yes [B12]             |
| Portkey OSS  | Yes [P2]                       | Yes [P2]                            | Partial [P3]      | Partial [P3]                        | Partial [P3]          |
| Helicone     | Partial [H9]                   | Partial [H9] [H10]                  | Yes [H7]          | Not documented                      | Yes [H7]              |
| TensorZero   | Yes [T4]                       | Yes [T4]                            | Yes [T2]          | Partial [T2]                        | Yes [T2]              |
| Agent Router | Partial [E2]                   | Partial [E2]                        | Partial [E4]      | Not documented                      | Yes [E4]              |
| agentgateway | Yes [A2]                       | Partial [A2]                        | Yes [A2]          | Partial [A2]                        | Yes [A2]              |
| Kong         | Partial [K2]                   | Partial [K2]                        | Partial [K2]      | Not documented                      | Partial [K2]          |
| LiteLLM      | Yes [L2]                       | Yes [L2]                            | Yes [L2]          | Yes [L4]                            | Yes [L4]              |
| Higress      | Yes [I2]                       | Partial [I2]                        | Yes [I2]          | Partial [I2]                        | Yes [I2]              |

S1 is the future Claude subscription path in the fixed plan. It is not scored as a current Anthropic API key route. S2 means stable caller-visible model/route names, not a requirement for arbitrary HTTP URL rewriting.

## Keys, traces, and operations

| Candidate    | M10 Declarative virtual-key issuance/rotation and trace metadata | M11 BuildBuddy key sync | M12 Structured durable traces, complete fields, and no prompt copies in pod logs | M13 Prometheus metrics | M14 Health endpoints | M15 Two-replica availability and state |
| ------------ | ---------------------------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------- | ---------------------- | -------------------- | -------------------------------------- |
| Bifrost      | Partial [B3]                                                     | Not documented          | Partial [B4] [B6]                                                                | Yes [B8]               | Yes [B5] [B11]       | Yes [B5] [B11]                         |
| Portkey OSS  | Not documented                                                   | Not documented          | Not documented                                                                   | Not documented         | Not documented       | Not documented                         |
| Helicone     | Not documented                                                   | Not documented          | Partial [H1] [H3]                                                                | Partial [H3]           | Yes [H8]             | Partial [H2]                           |
| TensorZero   | Partial [T3]                                                     | Not documented          | Partial [T5]                                                                     | Yes [T6]               | Yes [T6]             | Partial [T5]                           |
| Agent Router | Not documented                                                   | Not documented          | Partial [E2]                                                                     | Yes [E6]               | Yes [E5]             | Not documented                         |
| agentgateway | Not documented                                                   | Not documented          | Partial [A3] [A4]                                                                | Yes [A5]               | Yes [A5]             | Yes [A5]                               |
| Kong         | Not documented                                                   | Not documented          | Partial [K2] [K4]                                                                | Yes [K3]               | Yes [K5]             | Not documented                         |
| LiteLLM      | Yes [L4]                                                         | Yes [L4]                | Partial [L3] [L4] [L9] [L10]                                                     | Yes [L2]               | Yes [L2]             | Yes [L4]                               |
| Higress      | Not documented                                                   | Not documented          | Not documented                                                                   | Yes [I3]               | Yes [I3]             | Yes [I3]                               |

M12 is intentionally strict. A generic OTLP exporter does not by itself prove caller metadata, durable retention through a node outage, or that prompt bodies stay out of pod logs. LiteLLM's duplicate console export is addressed and its tested request reached Langfuse without the marker in sampled pod logs [L9] [L10], but caller/evaluation metadata and retention across a node loss have not been revalidated end to end on the new exporter. “Partial” means at least one of those Must details remains unproven. “Not documented” is not a claim that a feature cannot be added.

### Trace destination and Langfuse path

| Candidate    | Documented trace/store path                                                                                                                                                                                                                                                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bifrost      | Its LogStore can persist request/response records in PostgreSQL or ClickHouse. Its OTel plugin exports GenAI semantic-convention spans to a configurable OTLP collector; this is a generic OTLP path, not a Bifrost-specific Langfuse integration. To use Langfuse, configure its OTLP ingestion endpoint, /api/public/otel [B4] [B6] [A3]. |
| Portkey OSS  | The gateway builds request/response log objects, but the checked OSS sources do not document a local durable store or a Langfuse ingestion path [P4].                                                                                                                                                                                       |
| Helicone     | The README describes built-in Helicone logging and OpenTelemetry logs/metrics/traces. The checked gateway sources do not document a self-hosted durable trace backend or a Langfuse endpoint [H1] [H3].                                                                                                                                     |
| TensorZero   | Inference records are stored in its ClickHouse-backed data store; it can also export OTLP traces. Langfuse is not required for the bundled store, and a direct Langfuse ingestion path is not documented [T5] [T6].                                                                                                                         |
| Agent Router | Exports OpenInference GenAI spans over OTLP; its tracing example uses Phoenix. A Langfuse deployment can be an OTLP destination at /api/public/otel, but no native Langfuse integration is documented [E2] [A3].                                                                                                                            |
| agentgateway | Its example sends OTLP/HTTP directly to Langfuse at /api/public/otel and explicitly maps prompt, completion, and usage attributes. Those content fields are opt-in in the example. A separate example stores request logs in PostgreSQL; use one path for the Must rather than dual-writing prompt content [A3] [A4].                       |
| Kong         | The checked AI Proxy and telemetry sources do not establish a durable request-trace store or a Langfuse ingestion path [K2] [K3].                                                                                                                                                                                                           |
| LiteLLM      | The generic `otel` callback uses the Langfuse mapper and one OTLP/HTTP exporter at `/api/public/otel`; ESO composes headers into an optional Secret. If absent, the accepted fallback is console export [L9]. A live `/v1/responses` probe reached Langfuse, with no marker in either replica's log tail [L10].                             |
| Higress      | A durable LLM trace store, GenAI OTEL export, and Langfuse ingestion path are not documented in the checked sources.                                                                                                                                                                                                                        |

## Candidate assessment

**Bifrost.** The closest new challenger: the checked release documents Anthropic, Gemini, Ollama, and OpenAI-compatible providers; a PostgreSQL-backed ConfigStore and LogStore; virtual keys; Prometheus; and Helm deployment [B2] [B3] [B4] [B5] [B6] [B11]. Its public docs describe management APIs for virtual keys, but do not establish a Kubernetes-native declarative reconciler for them [B3]. The exact root /v1/messages route, Messages-to-CLIProxyAPI behavior, trace caller metadata, and safe logging need a spike [B7] [B6]. Effort: moderate, mostly control-plane integration and compatibility proof; patching core code may be avoidable if its APIs/configuration suffice.

**Portkey AI Gateway OSS.** Its route table is the strongest documented API-shape match: it registers /v1/responses, /v1/chat/completions, /v1/messages, /v1/embeddings, and /v1/models [P2]. The OSS gateway is self-hostable [P3], but the checked repository does not document OSS-owned virtual-key lifecycle, a local durable trace store, Prometheus scraping, or HA state [P3] [P4]. Effort: high unless separate control-plane and trace components already fit; an upstream adapter may be needed for exact CLIProxyAPI lanes.

**Helicone AI Gateway.** The Rust gateway is self-hostable and documents OpenAI-compatible and Anthropic paths, provider routing, telemetry, and model mapping [H1] [H2] [H3] [H7]. The checked source does not establish the full five-path contract or a self-hosted durable trace backend for the gateway alone [H2] [H3]. Its repository has been inactive since November 2025, and LICENSE conflicts with its README badge [H1] [H4]. Effort: high, with route/trace gaps and license clarification before adoption.

**TensorZero.** Its bundled ClickHouse inference store and PostgreSQL API-key service are useful, and the project documents a broad provider roster, streaming, tools, and evaluation metadata [T2] [T3] [T4] [T5]. Its checked OpenAI-compatible route registry contains only /openai/v1/chat/completions and /openai/v1/embeddings; the Responses guide still uses chat completions at /openai/v1 [T7] [T10]. The repository is archived [T1]. Effort: high because the API compatibility gap is central and upstream maintenance is stopped.

**Agent Router (formerly Envoy AI Gateway).** Strong Kubernetes-native routes, upstream credential Secrets, full-request OpenInference spans, Prometheus metrics, and health checks [E2] [E3] [E4] [E5] [E6]. The documented traced API surface is chat completions, legacy completions, and embeddings; client virtual-key issuance and a bundled durable trace store are not established [E2] [E3]. Effort: high for the unverified client APIs and key lifecycle; medium for a design that delegates those functions to existing Kubernetes controllers and an OTEL backend.

**agentgateway.** The strongest infrastructure-focused alternative: Kubernetes installation, model aliases, OpenAI/Anthropic/Ollama examples, direct Langfuse OTLP configuration, and a separate Postgres request-log example [A2] [A3] [A4] [A5]. Its checked sources do not document virtual-key issuance with per-key trace metadata, and the five exact root endpoints still need verification [A2]. Effort: moderate to high, depending on whether an external key issuer can supply the required metadata without a gateway patch.

**Kong AI Gateway.** Self-hostable and operationally mature, with API-key authentication mechanisms, streaming AI proxy support, health, and Prometheus [K1] [K2] [K3] [K5]. The checked 3.9.4 AI Proxy schema centers on chat-style routes and lists OpenAI, Bedrock, and Gemini formats; it does not establish the required Responses plus Messages client lanes, GenAI trace completeness, or virtual-key metadata in traces [K2] [K4]. Effort: high for protocol adapters and trace/key integration; verify the exact OSS package and feature set during any spike.

**LiteLLM, stay on current.** The deployed 1.103.0 has the smallest API migration surface: all five paths, the current provider roster, both CLIProxyAPI lanes, model aliases, Crossplane-managed keys, Postgres-backed availability, and the existing BuildBuddy secret sync are already represented in the checked configuration [L2] [L4]. The duplicate console-to-Loki export is addressed by merged PR #11951: a generic `otel` callback with the Langfuse mapper selects one OTLP/HTTP exporter at `/api/public/otel`, and ESO composes its headers [L9]. The post-change probe confirmed the request in Langfuse and did not find its marker in either replica's sampled pod logs [L10]. If the optional header Secret is missing, LiteLLM falls back to console export, which the owner accepts for Langfuse outages [L9]. M12 remains partial until caller/evaluation metadata and node-loss retention are verified on the new exporter. Existing key ownership still depends on a patched Crossplane provider [L4]. Effort: low for the current trace path; the keep-in-place spike should focus on the remaining API, caller-metadata, retention, catalog, and key-rotation checks.

**Higress (additional candidate).** The self-hosted AI Proxy documents OpenAI chat, Anthropic Messages, embeddings, model mapping, and streaming [I2] [I3]. The checked source does not establish Responses, model discovery, complete traces, or declarative virtual-key metadata [I2] [I3]. Effort: high because compatibility and trace work would be required before it could replace the current API surface.

## Top two to spike

1. **LiteLLM keep-in-place spike.** The duplicate console-to-Loki path was removed in merged PR #11951, and a marker-bearing `/v1/responses` probe now appears in Langfuse without its marker in either replica's sampled pod logs [L9] [L10]. LiteLLM remains the strongest keep-in-place option because all current client paths, provider/auth routes, key ownership, BuildBuddy sync, and two-replica state are already represented in the checked environment [L4]. Prove the remaining Must behavior with streamed Responses and Messages calls: input, output, model, usage, and caller/evaluation metadata in the durable store; no duplicate prompt/output logs with the OTLP Secret present; and retention after a node loss. Verify the documented fallback to console export when the optional OTLP Secret is absent, which the owner accepts for Langfuse outages [L9]. Also prove Claude discovery with `GET /v1/models?limit=1000`, Codex's `model_catalog_json` alignment with each caller's authorized models, and BuildBuddy key refresh after rotation without exposing key material.

2. **Bifrost replacement spike.** It is the best new candidate because it documents a durable PostgreSQL LogStore, virtual keys, provider breadth, and Prometheus rather than requiring a separate trace database by default [B3] [B4] [B8]. Prove the exact `/v1/responses` and root `/v1/messages` paths against CLIProxyAPI using its OpenAI-compatible bearer-key upstream, with streaming and tool calls; direct Anthropic and Gemini keys; Ollama and Rugged's local OpenAI-compatible route; a declarative virtual-key create/rotate flow with evaluation and caller metadata reaching the trace; BuildBuddy secret refresh on rotation; durable single-store retention across a node loss; no prompt/output in pod logs; Prometheus and health endpoints; and two replicas. Specifically test Claude's `GET /v1/models?limit=1000`: its response has the expected `data[].id` shape, but Bifrost parses pagination as `page_size`/`page_token`, so `limit` behavior must be verified. Prove how Codex's separate `model_catalog_json` stays aligned with the gateway's per-caller authorized model list. Capture any required patch or controller work, including the exact Helm shared-state configuration.

The LiteLLM spike confirms the remaining end-to-end Must behavior for a keep-in-place path; the Bifrost spike tests replacement. LiteLLM's trace exporter now has a live `/v1/responses` check, while both candidates still need the broader API, metadata, durability, and key lifecycle proof above. The other candidates have more than one documented Must gap or a larger unverified key/trace surface.

## Client model catalog compatibility

The clients use different model-list contracts. Claude Code's opt-in gateway discovery requests `GET /v1/models?limit=1000` and reads `data[]` entries with `id` plus optional `display_name` and `description` [G9]. Codex CLI instead expects a `ModelsResponse` object with a top-level `models` array of `ModelInfo` entries, including a `slug` and Codex-specific metadata. It fetches this from `/models` or a configured `model_catalog_url`, and can also load a local JSON file through `model_catalog_json` [C1] [C2] [C3] [C4] [C5]. A conventional OpenAI model list does not satisfy Codex's schema.

The table scores the checked gateway's own response path and schema, not whether a sidecar could be added. “Partial” means the endpoint exists but the OSS source delegates the catalog response or does not establish its contract. “Not documented” means the checked source does not establish a gateway-owned endpoint and schema. The Claude column is specifically the discovery shape above; discovery remains opt-in in Claude Code.

| Candidate                                | Claude Code discovery: `GET /v1/models` | Codex CLI native catalog: `GET /models` | Checked serving behavior                                                                                                                                                                                                                       |
| ---------------------------------------- | --------------------------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bifrost                                  | Partial [B13]                           | No [B13]                                | `/v1/models` returns a `data` list whose model entries carry `id`, but the Anthropic `limit` query is not parsed as pagination (`page_size` and `page_token` are); the checked handler registers `/v1/models`, not Codex's `/models` response. |
| Portkey AI Gateway OSS                   | Partial [P2] [P7]                       | Not documented [P2] [P7]                | OSS registers `/v1/models`, but the handler delegates to the configured Albus control plane or the next provider handler; the OSS source does not establish the returned catalog schema.                                                       |
| Helicone AI Gateway                      | Not documented [H2]                     | Not documented [H2]                     | The checked gateway route source does not establish either client catalog contract.                                                                                                                                                            |
| TensorZero                               | No [T11]                                | No [T11]                                | The checked gateway route registry exposes chat completions and embeddings, but no `/v1/models` or `/models` catalog route.                                                                                                                    |
| Agent Router (formerly Envoy AI Gateway) | Yes [E9]                                | No [E9]                                 | The model-list processors produce Anthropic and OpenAI `data` lists for `/v1/models`; no Codex `ModelsResponse` path is documented.                                                                                                            |
| agentgateway                             | Yes [A8]                                | No [A8]                                 | Both `/v1/models` and `/models` are served from its configured model routes as `{"data":[{"id":...}],"object":"list"}`; `/models` is not Codex's native schema.                                                                                |
| Kong AI Gateway                          | Not documented [K2]                     | Not documented [K2]                     | The checked AI Proxy source does not establish either gateway-owned catalog contract.                                                                                                                                                          |
| LiteLLM, stay on current                 | Yes [L8]                                | No [L8]                                 | It registers both paths; Claude Code requests receive an Anthropic model-list envelope, while `/models` returns `data[]`, not Codex's `models[]`.                                                                                              |
| Higress                                  | Not documented [I2]                     | Not documented [I2]                     | The checked AI Proxy source does not establish either gateway-owned catalog contract.                                                                                                                                                          |

For the leading two spikes, LiteLLM has the documented Claude discovery branch; Bifrost's `/v1/models` response has the required `data[].id` shape, but its `limit=1000` behavior still needs proof. Neither provides Codex's native `/models` schema in the checked source. Codex can use its own `model_catalog_json` setting, but the spike should prove that the operator-managed catalog stays aligned with each caller's authorized gateway models [B13] [C4] [C5] [L8].

## Claude Apps Gateway

Anthropic's Claude Apps Gateway is a self-hosted service built into the `claude` binary; the gateway command first shipped in Claude Code v2.1.195, and the checked release examples are pinned to v2.1.310 [G1] [G2] [G4] [G7]. Developers sign in through the gateway's OIDC-backed SSO flow and receive short-lived bearer tokens. Clients use Anthropic Messages at `POST /v1/messages`; the gateway translates to configured Anthropic API, Bedrock/Mantle, Claude Platform on AWS, Google Cloud Agent Platform, or Microsoft Foundry upstreams [G7] [G9]. The YAML `models` catalog is served at `GET /v1/models` and maps stable client IDs to provider-specific upstream IDs. Managed policies can limit a user's models, and the gateway enforces that allowlist on inference [G8].

Separately, Anthropic's compatibility guide says Claude Code can opt into model discovery for a gateway configured with `ANTHROPIC_BASE_URL` by setting `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`: it sends `GET /v1/models?limit=1000` and expects `data[]` entries with `id` and optional `display_name` and `description` [G9]. Discovery is off by default. The static docs do not say that this exact discovery flow is used for Apps Gateway sign-in sessions; for its complete deployed endpoint list, Anthropic directs operators to that instance's `GET /protocol` [G7].

The checked docs describe a Claude Code gateway using Anthropic Messages; they do not document `/v1/responses`, `/v1/chat/completions`, `/v1/embeddings`, or a generic OpenAI-compatible upstream such as CLIProxyAPI [G7] [G9]. That leaves it outside the replacement scorecard against the fixed API and provider requirements. Anthropic says a deployed Apps Gateway exposes `GET /protocol`, a per-deployment protocol reference covering sign-in, inference, managed settings, model discovery, and telemetry endpoints; the static docs do not list that full version-specific endpoint inventory [G7]. The local Claude Code wrapper also opts into gateway model discovery [G6].

## Open questions carried from the fixed plan

- Which optional provider routes remain cost-effective: Groq, Mistral, z.ai/GLM, Antigravity, and Tana? Tana may remain a separate LiteLLM instance.
- If optional per-key limits are used, what token threshold and time window should apply? USD limits do not fit subscription or local-backend costs.
- How should Codex's native catalog stay aligned with each caller's gateway-authorized models? The source check establishes LiteLLM's Claude discovery response; Bifrost's `data[].id` shape matches, but its `limit=1000` handling is unproven. No checked candidate serves Codex's native `ModelsResponse`; Codex supports a separately configured `model_catalog_json` or `model_catalog_url` [B13] [L8] [C1] [C2] [C3] [C4] [C5] [G9]. The spike should prove the chosen catalog's authentication and per-caller filtering behavior.
- For the winning gateway, does its route metadata preserve the evaluation fields sent by Loom Gym and the skills evaluation callers without changing their clients?

## Primary sources

Candidate source pages and GitHub release/repository metadata below were checked on 2026-10-24 UTC, except Codex CLI sources, Claude Code gateway sources, and the LiteLLM exporter change and runtime validation, which were checked on 2026-10-25 UTC. Version-specific source links are pinned to the assessed release/tag where one exists. Helicone is pinned to the checked main commit because no release was found; Codex CLI is pinned to commit `970111f6cb62cc02fe36a82ea7d71b37a2889c97`; Claude Code examples are pinned to v2.1.310, released 2026-10-23. Anthropic's current gateway docs are living docs checked on 2026-10-25 UTC; they say the gateway command requires Claude Code v2.1.195 or later.

Gateway model-list handlers and schemas used in the compatibility table were checked on 2026-10-25 UTC.

- [B1] Bifrost release, repository, README, and license: <https://github.com/maximhq/bifrost/releases/tag/ent-v2.3.2-base>
- [B2] Bifrost provider documentation at the assessed tag: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/core/providers.mdx>
- [B3] Bifrost virtual-key management documentation at the assessed tag: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/features/governance/virtual-keys.mdx>
- [B4] Bifrost LogStore documentation at the assessed tag: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/framework/log-store.mdx>
- [B5] Bifrost Helm chart and deployment guidance at the assessed tag: <https://github.com/maximhq/bifrost/tree/ent-v2.3.2-base/helm-charts/bifrost>
- [B6] Bifrost OTEL/GenAI observability documentation at the assessed tag: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/features/observability/otel.mdx>
- [B7] Bifrost API and streaming documentation: <https://github.com/maximhq/bifrost/tree/ent-v2.3.2-base/docs/quickstart/gateway>
- [B8] Bifrost Prometheus metrics documentation: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/features/observability/prometheus.mdx>
- [B9] Bifrost license: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/LICENSE>
- [B10] Bifrost development-branch commit history: <https://github.com/maximhq/bifrost/commits/dev/>
- [B11] Bifrost ConfigStore documentation, including PostgreSQL and virtual-key configuration: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/framework/config-store.mdx>
- [B12] Bifrost model catalog documentation: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/framework/model-catalog.mdx>
- [B13] Bifrost v2.3.2-base model-list route, query handling, and response schema: <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/transports/bifrost-http/handlers/inference.go#L864>, <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/transports/bifrost-http/handlers/inference.go#L993>, <https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/core/schemas/models.go#L72>
- [P1] Portkey v1.15.2 release: <https://github.com/Portkey-AI/gateway/releases/tag/v1.15.2>
- [P2] Portkey v1.15.2 route registration and request handlers: <https://github.com/Portkey-AI/gateway/blob/v1.15.2/src/index.ts>
- [P3] Portkey v1.15.2 README, provider setup, and self-hosted deployment: <https://github.com/Portkey-AI/gateway/tree/v1.15.2>
- [P4] Portkey v1.15.2 request logging service: <https://github.com/Portkey-AI/gateway/blob/v1.15.2/src/handlers/services/logsService.ts>
- [P5] Portkey license: <https://github.com/Portkey-AI/gateway/blob/v1.15.2/LICENSE>
- [P6] Portkey main-branch commit history: <https://github.com/Portkey-AI/gateway/commits/main/>
- [P7] Portkey v1.15.2 model-list route and OSS handler: <https://github.com/Portkey-AI/gateway/blob/v1.15.2/src/index.ts#L102>, <https://github.com/Portkey-AI/gateway/blob/v1.15.2/src/handlers/modelsHandler.ts>
- [H1] Helicone checked main commit and README: <https://github.com/Helicone/ai-gateway/tree/9649b27bdc9fb0907d359e899894102a15f3a085>
- [H2] Helicone route registration source at the checked commit: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/app.rs>
- [H3] Helicone telemetry and provider configuration at the checked commit: <https://github.com/Helicone/ai-gateway/tree/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src>
- [H4] Helicone checked LICENSE file: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/LICENSE>
- [H5] Helicone AI Gateway releases: <https://github.com/Helicone/ai-gateway/releases>
- [H6] Helicone authentication source: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/middleware/auth.rs>
- [H7] Helicone model-mapping source: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/config/model_mapping.rs>
- [H8] Helicone health-check source: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/utils/health_check.rs>
- [H9] Helicone OpenAI Chat Completions handler: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/endpoints/openai/chat_completions.rs>
- [H10] Helicone Anthropic Messages handler: <https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/endpoints/anthropic/messages.rs>
- [T1] TensorZero 2026.6.0 release: <https://github.com/tensorzero/tensorzero/releases/tag/2026.6.0>
- [T2] TensorZero 2026.6.0 provider roster, model configuration, and self-hosted gateway: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/README.md>
- [T3] TensorZero 2026.6.0 API-key service and PostgreSQL schema: <https://github.com/tensorzero/tensorzero/tree/2026.6.0/crates/tensorzero-auth>
- [T4] TensorZero 2026.6.0 OpenAI-compatible chat-completions API: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/docs/gateway/api-reference/inference-openai-compatible.mdx>
- [T5] TensorZero 2026.6.0 ClickHouse inference storage: <https://github.com/tensorzero/tensorzero/tree/2026.6.0/crates/tensorzero-core/src/db/clickhouse>
- [T6] TensorZero 2026.6.0 Prometheus, health, and OTLP routes: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/crates/gateway/src/routes/external.rs>
- [T7] TensorZero 2026.6.0 OpenAI Responses guide: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/docs/gateway/call-the-openai-responses-api.mdx>
- [T8] TensorZero license: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/LICENSE>
- [T9] TensorZero repository status and archive metadata: <https://github.com/tensorzero/tensorzero>
- [T10] TensorZero 2026.6.0 OpenAI-compatible route registry: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/crates/tensorzero-core/src/endpoints/openai_compatible/mod.rs>
- [T11] TensorZero 2026.6.0 complete external route construction and OpenAI-compatible route registry: <https://github.com/tensorzero/tensorzero/blob/2026.6.0/crates/gateway/src/routes/external.rs#L24>, <https://github.com/tensorzero/tensorzero/blob/2026.6.0/crates/tensorzero-core/src/endpoints/openai_compatible/mod.rs#L27>
- [E1] Agent Router v1.2.0 release: <https://github.com/theagentrouter/agent-router/releases/tag/v1.2.0>
- [E2] Agent Router v1.2.0 GenAI tracing documentation: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/site/docs/capabilities/observability/tracing.md>
- [E3] Agent Router v1.2.0 upstream-auth documentation: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/site/docs/capabilities/security/upstream-auth.mdx>
- [E4] Agent Router v1.2.0 Kubernetes route API and CRD: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/api/v1beta1/ai_gateway_route.go>
- [E5] Agent Router v1.2.0 health-check implementation: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/cmd/aigw/healthcheck.go>
- [E6] Agent Router v1.2.0 Prometheus metrics documentation: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/site/docs/capabilities/observability/metrics.md>
- [E7] Agent Router license: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/LICENSE>
- [E8] Agent Router main-branch commit history: <https://github.com/theagentrouter/agent-router/commits/main/>
- [E9] Agent Router v1.2.0 OpenAI and Anthropic model-list processors and response tests: <https://github.com/theagentrouter/agent-router/blob/v1.2.0/internal/extproc/models_processor.go#L27>, <https://github.com/theagentrouter/agent-router/blob/v1.2.0/internal/extproc/models_processor.go#L96>, <https://github.com/theagentrouter/agent-router/blob/v1.2.0/internal/extproc/models_processor_test.go>
- [A1] agentgateway v1.6.0 release: <https://github.com/agentgateway/agentgateway/releases/tag/v1.6.0>
- [A2] agentgateway v1.6.0 OpenAI, Anthropic, Ollama, and alias example: <https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/llm-basic/README.md>
- [A3] agentgateway v1.6.0 Langfuse OTLP/HTTP example: <https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/llm-telemetry/tracing/langfuse.yaml>
- [A4] agentgateway v1.6.0 PostgreSQL request-log example: <https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/llm-ollama-postgres/README.md>
- [A5] agentgateway v1.6.0 Helm charts, probes, and metrics configuration: <https://github.com/agentgateway/agentgateway/tree/v1.6.0/controller/install/helm>
- [A6] agentgateway license: <https://github.com/agentgateway/agentgateway/blob/v1.6.0/LICENSE>
- [A7] agentgateway main-branch commit history: <https://github.com/agentgateway/agentgateway/commits/main/>
- [A8] agentgateway v1.6.0 model-list route recognition, authorization, and response schema: <https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/llm/model_router.rs#L67>, <https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/llm/model_router.rs#L328>, <https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/llm/model_router.rs#L373>
- [K1] Kong Gateway 3.9.4 release: <https://github.com/Kong/kong/releases/tag/3.9.4>
- [K2] Kong 3.9.4 AI Proxy schema and streaming integration coverage: <https://github.com/Kong/kong/tree/3.9.4/kong/plugins/ai-proxy>
- [K3] Kong 3.9.4 Prometheus plugin and health endpoints: <https://github.com/Kong/kong/tree/3.9.4/kong/plugins/prometheus>
- [K4] Kong 3.9.4 AI Proxy telemetry release note: <https://github.com/Kong/kong/blob/3.9.4/changelog/3.6.0/kong/add-ai-proxy-telemetry.yml>
- [K5] Kong 3.9.4 health endpoint: <https://github.com/Kong/kong/blob/3.9.4/kong/api/routes/health.lua>
- [K6] Kong core license: <https://github.com/Kong/kong/blob/3.9.4/LICENSE>
- [K7] Kong master-branch commit history: <https://github.com/Kong/kong/commits/master/>
- [L1] LiteLLM v1.103.0 release: <https://github.com/BerriAI/litellm/releases/tag/v1.103.0>
- [L2] LiteLLM proxy API, providers, streaming, health, and metrics documentation: <https://docs.litellm.ai/docs/>
- [L3] LiteLLM Langfuse integration documentation: <https://docs.litellm.ai/docs/observability/langfuse>
- [L4] Current checked-in routes, tracing settings, Crossplane keys, and BuildBuddy sync in the fixed replacement plan: [llm_gateway_replacement.md](llm_gateway_replacement.md), [proxy.py](../../../cdk8s/litellm/proxy.py), [Crossplane key definitions](../../../cdk8s/litellm/crossplane_keys.py), [BuildBuddy sync](../../../../tf/gitops/litellm-buildbuddy-sync/main.tf)
- [L5] LiteLLM core and enterprise license boundary: <https://github.com/BerriAI/litellm/blob/v1.103.0/LICENSE>
- [L6] LiteLLM v1.106.3 latest release: <https://github.com/BerriAI/litellm/releases/tag/v1.106.3>
- [L7] LiteLLM main-branch commit history: <https://github.com/BerriAI/litellm/commits/main/>
- [L8] LiteLLM v1.103.0 model-list route, Anthropic-format branch, and response schema: <https://github.com/BerriAI/litellm/blob/v1.103.0/litellm/proxy/proxy_server.py#L10892>, <https://github.com/BerriAI/litellm/blob/v1.103.0/litellm/proxy/proxy_server.py#L11053>, <https://github.com/BerriAI/litellm/blob/v1.103.0/litellm/llms/anthropic/common_utils.py#L1734>
- [L9] Merged PR replacing LiteLLM's Langfuse preset with one generic OTLP exporter, including the optional ESO-composed headers and console fallback: <https://github.com/agentydragon/ducktape/pull/11951>
- [L10] 2026-10-25 live `/v1/responses` trace and both-replica pod-log validation for issue #11937: <https://github.com/agentydragon/ducktape/issues/11937#issuecomment-6393833026>
- [I1] Higress v2.2.5 release: <https://github.com/higress-group/higress/releases/tag/v2.2.5>
- [I2] Higress v2.2.5 AI Proxy provider, path, and model-mapping documentation: <https://github.com/higress-group/higress/blob/v2.2.5/plugins/wasm-go/extensions/ai-proxy/README_EN.md>
- [I3] Higress v2.2.5 Helm deployment and monitoring configuration: <https://github.com/higress-group/higress/tree/v2.2.5/helm/core>
- [I4] Higress license: <https://github.com/higress-group/higress/blob/v2.2.5/LICENSE>
- [I5] Higress main-branch commit history: <https://github.com/higress-group/higress/commits/main/>
- [C1] Codex CLI native `ModelInfo` and `ModelsResponse` types at the checked commit: <https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/protocol/src/openai_models.rs#L411>
- [C2] Codex CLI `/models` client and `ModelsResponse` decoding: <https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/codex-api/src/endpoint/models.rs#L33>
- [C3] Codex CLI remote model-catalog URL and slug validation: <https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/model-provider/src/models_endpoint.rs#L45>
- [C4] Codex CLI `model_catalog_json` config setting: <https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/config/src/config_toml.rs#L408>
- [C5] Codex CLI static catalog JSON loading and `ModelsResponse` decoding: <https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/config/mod.rs#L2224>
- [G1] Anthropic Claude Code v2.1.310 release: <https://github.com/anthropics/claude-code/releases/tag/v2.1.310>
- [G2] Anthropic Claude Apps Gateway AWS deployment example: <https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/aws/README.md>
- [G3] Anthropic Claude Apps Gateway AWS model and upstream configuration: <https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/aws/gateway.yaml.example>
- [G4] Anthropic Claude Gateway GCP deployment example: <https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/gcp/README.md>
- [G5] Anthropic Claude Gateway GCP model and upstream configuration: <https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/gcp/gateway.yaml.example>
- [G6] This repository's Claude Code gateway wrapper and model discovery setting: [gateway.nix](../../../../nix/home/claude_code/gateway.nix)
- [G7] Anthropic Claude Apps Gateway overview, supported upstreams, and per-deployment `GET /protocol` reference: <https://code.claude.com/docs/en/claude-apps-gateway>
- [G8] Anthropic Claude Apps Gateway configuration: model catalog served at `/v1/models` and managed model access: <https://code.claude.com/docs/en/claude-apps-gateway-config#models>
- [G9] Anthropic Claude Code gateway compatibility guide: Messages paths and `/v1/models` discovery request and response shape: <https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery>

[B1]: https://github.com/maximhq/bifrost/releases/tag/ent-v2.3.2-base
[B2]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/core/providers.mdx
[B3]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/features/governance/virtual-keys.mdx
[B4]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/framework/log-store.mdx
[B5]: https://github.com/maximhq/bifrost/tree/ent-v2.3.2-base/helm-charts/bifrost
[B6]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/features/observability/otel.mdx
[B7]: https://github.com/maximhq/bifrost/tree/ent-v2.3.2-base/docs/quickstart/gateway
[B8]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/features/observability/prometheus.mdx
[B9]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/LICENSE
[B10]: https://github.com/maximhq/bifrost/commits/dev/
[B11]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/framework/config-store.mdx
[B12]: https://github.com/maximhq/bifrost/blob/ent-v2.3.2-base/docs/architecture/framework/model-catalog.mdx
[P1]: https://github.com/Portkey-AI/gateway/releases/tag/v1.15.2
[P2]: https://github.com/Portkey-AI/gateway/blob/v1.15.2/src/index.ts
[P3]: https://github.com/Portkey-AI/gateway/tree/v1.15.2
[P4]: https://github.com/Portkey-AI/gateway/blob/v1.15.2/src/handlers/services/logsService.ts
[P5]: https://github.com/Portkey-AI/gateway/blob/v1.15.2/LICENSE
[P6]: https://github.com/Portkey-AI/gateway/commits/main/
[H1]: https://github.com/Helicone/ai-gateway/tree/9649b27bdc9fb0907d359e899894102a15f3a085
[H2]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/app.rs
[H3]: https://github.com/Helicone/ai-gateway/tree/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src
[H4]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/LICENSE
[H5]: https://github.com/Helicone/ai-gateway/releases
[H6]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/middleware/auth.rs
[H7]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/config/model_mapping.rs
[H8]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/utils/health_check.rs
[H9]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/endpoints/openai/chat_completions.rs
[H10]: https://github.com/Helicone/ai-gateway/blob/9649b27bdc9fb0907d359e899894102a15f3a085/ai-gateway/src/endpoints/anthropic/messages.rs
[T1]: https://github.com/tensorzero/tensorzero/releases/tag/2026.6.0
[T2]: https://github.com/tensorzero/tensorzero/blob/2026.6.0/README.md
[T3]: https://github.com/tensorzero/tensorzero/tree/2026.6.0/crates/tensorzero-auth
[T4]: https://github.com/tensorzero/tensorzero/blob/2026.6.0/docs/gateway/api-reference/inference-openai-compatible.mdx
[T5]: https://github.com/tensorzero/tensorzero/tree/2026.6.0/crates/tensorzero-core/src/db/clickhouse
[T6]: https://github.com/tensorzero/tensorzero/blob/2026.6.0/crates/gateway/src/routes/external.rs
[T7]: https://github.com/tensorzero/tensorzero/blob/2026.6.0/docs/gateway/call-the-openai-responses-api.mdx
[T8]: https://github.com/tensorzero/tensorzero/blob/2026.6.0/LICENSE
[T9]: https://github.com/tensorzero/tensorzero
[T10]: https://github.com/tensorzero/tensorzero/blob/2026.6.0/crates/tensorzero-core/src/endpoints/openai_compatible/mod.rs
[E1]: https://github.com/theagentrouter/agent-router/releases/tag/v1.2.0
[E2]: https://github.com/theagentrouter/agent-router/blob/v1.2.0/site/docs/capabilities/observability/tracing.md
[E3]: https://github.com/theagentrouter/agent-router/blob/v1.2.0/site/docs/capabilities/security/upstream-auth.mdx
[E4]: https://github.com/theagentrouter/agent-router/blob/v1.2.0/api/v1beta1/ai_gateway_route.go
[E5]: https://github.com/theagentrouter/agent-router/blob/v1.2.0/cmd/aigw/healthcheck.go
[E6]: https://github.com/theagentrouter/agent-router/blob/v1.2.0/site/docs/capabilities/observability/metrics.md
[E7]: https://github.com/theagentrouter/agent-router/blob/v1.2.0/LICENSE
[E8]: https://github.com/theagentrouter/agent-router/commits/main/
[A1]: https://github.com/agentgateway/agentgateway/releases/tag/v1.6.0
[A2]: https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/llm-basic/README.md
[A3]: https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/llm-telemetry/tracing/langfuse.yaml
[A4]: https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/llm-ollama-postgres/README.md
[A5]: https://github.com/agentgateway/agentgateway/tree/v1.6.0/controller/install/helm
[A6]: https://github.com/agentgateway/agentgateway/blob/v1.6.0/LICENSE
[A7]: https://github.com/agentgateway/agentgateway/commits/main/
[K1]: https://github.com/Kong/kong/releases/tag/3.9.4
[K2]: https://github.com/Kong/kong/tree/3.9.4/kong/plugins/ai-proxy
[K3]: https://github.com/Kong/kong/tree/3.9.4/kong/plugins/prometheus
[K4]: https://github.com/Kong/kong/blob/3.9.4/changelog/3.6.0/kong/add-ai-proxy-telemetry.yml
[K5]: https://github.com/Kong/kong/blob/3.9.4/kong/api/routes/health.lua
[K6]: https://github.com/Kong/kong/blob/3.9.4/LICENSE
[K7]: https://github.com/Kong/kong/commits/master/
[L1]: https://github.com/BerriAI/litellm/releases/tag/v1.103.0
[L2]: https://docs.litellm.ai/docs/
[L3]: https://docs.litellm.ai/docs/observability/langfuse
[L4]: llm_gateway_replacement.md
[L5]: https://github.com/BerriAI/litellm/blob/v1.103.0/LICENSE
[L6]: https://github.com/BerriAI/litellm/releases/tag/v1.106.3
[L7]: https://github.com/BerriAI/litellm/commits/main/
[I1]: https://github.com/higress-group/higress/releases/tag/v2.2.5
[I2]: https://github.com/higress-group/higress/blob/v2.2.5/plugins/wasm-go/extensions/ai-proxy/README_EN.md
[I3]: https://github.com/higress-group/higress/tree/v2.2.5/helm/core
[I4]: https://github.com/higress-group/higress/blob/v2.2.5/LICENSE
[I5]: https://github.com/higress-group/higress/commits/main/
[C1]: https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/protocol/src/openai_models.rs#L411
[C2]: https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/codex-api/src/endpoint/models.rs#L33
[C3]: https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/model-provider/src/models_endpoint.rs#L45
[C4]: https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/config/src/config_toml.rs#L408
[C5]: https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/config/mod.rs#L2224
[G1]: https://github.com/anthropics/claude-code/releases/tag/v2.1.310
[G2]: https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/aws/README.md
[G3]: https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/aws/gateway.yaml.example
[G4]: https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/gcp/README.md
[G5]: https://github.com/anthropics/claude-code/blob/v2.1.310/examples/gateway/gcp/gateway.yaml.example
[G6]: ../../../../nix/home/claude_code/gateway.nix
[G7]: https://code.claude.com/docs/en/claude-apps-gateway
[G8]: https://code.claude.com/docs/en/claude-apps-gateway-config#models
[G9]: https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery
