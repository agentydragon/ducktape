# LiteLLM catalogue and publication

Supporting evidence for the [model-roster design](design.md), using its
[shared token vocabulary](design.md#token-limit-vocabulary). Observations are dated
and version-scoped; they do not authorize configuration or client-budget changes.

## Evidence scope

The proxy's package-local lock and upstream image pin are LiteLLM **1.100.1**.
The root Python lock is not the proxy runtime. Relevant upstream source files are
`utils.py`, `router.py`, `proxy/proxy_server.py`,
`litellm_core_utils/get_model_cost_map.py`, and `llms/ollama/common_utils.py`.
The wheel's `model_prices_and_context_window_backup.json` and the
[remote catalogue](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
were inspected separately. The remote snapshot below was fetched 2026-10-05.

Unless configured otherwise, LiteLLM downloads the remote catalogue; the bundled
file is a fallback. `LITELLM_LOCAL_MODEL_COST_MAP=True` selects the bundled map.
Pinning the package alone does **not** pin the catalogue. Neither source is evidence
for our subscription gateway's limits.

## What happens to metadata

1. LiteLLM resolves `litellm_params.model`, including its adapter prefix, against
   catalogue candidates with provider-compatibility checks.
2. Exact misses can use catalogue regex generalizations. Native Ollama may query
   `/api/show`; there is more than a static JSON lookup involved.
3. Deployment `model_info` registers/merges overrides into LiteLLM's model-cost map.
   This map contains both capacity/capability and pricing metadata. Catalogue reload
   replaces the map, then `reapply_runtime_model_cost_registrations()` replays live
   router deployments and explicit runtime registrations; it is not a reset to
   catalogue-only metadata. Router replay serializes `model_info` with `exclude_none=True`.
4. Router objects and metadata endpoints perform further serialization and merging.
   List and single-deployment `/model/info` paths are not identical.

Omitting a key permits fallback. Explicit YAML `null` is **not a reliable deletion
operator**: `register_model()` uses `_update_dictionary()`, which ignores `None`;
router serialization uses `exclude_none=True`; the single-deployment metadata
endpoint removes nulls before filling missing keys. The final response merge alone
is therefore insufficient evidence that null suppression works.

## Publication ownership

**Prior agreed direction, under reconsideration as of 2026-10-10:** see the
[routing review](routing_review.md). Existing publication remains unchanged; do not
treat completion of this target as a prerequisite for that review.

Legacy and current fields may coexist. The requirement is that
related token values come from one consistent source, not that legacy keys disappear.
**Every served route's token metadata will be declared by Ducktape.** It is acceptable
to copy values from a specific LiteLLM catalogue entry, with revision/source comments
beside the declaration; the catalogue is a reference input, not an implicit runtime
second authority. We do not need independent live capacity probes for every model.

For generative routes marked `publish_limits`, the ordinary cdk8s projection emits:

- `max_input_tokens` from `Model.limits.max_input_tokens`.
- `max_output_tokens` and legacy `max_tokens` from the **same**
  `Model.limits.max_output_tokens` declaration.

In pinned 1.100.1, [`get_max_tokens()`](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/utils.py#L5149-L5215)
is documented as an output limit lookup and falls back from `max_output_tokens` to
`max_tokens`. This justifies that compatibility mapping for our generative overrides;
it is not a universal interpretation of every provider's `maxTokens` or of embedding
metadata. The source-to-LiteLLM mapping carries a short comment with this rationale.
No third independent limit belongs in the neutral roster; do not restore the unused
custom `context_window` metadata or change request-body generation caps.

The reworked #9273 uses **unmodified LiteLLM and ordinary config**. The earlier draft's
downstream patch, publication marker, response projection and image overlay are dropped.
That slice preserved input/output numbers and publication choices. The direct-provider
expansion below adds sourced declarations; it does not validate the inherited ChatGPT
allowances merely by making their publication consistent. Prices and capability
metadata retain their existing ownership; this change concerns token-limit metadata.

**Incomplete migration, not an unresolved ownership policy:** routes without overrides
still use LiteLLM's catalogue/adapter fallback today. That is not an accepted end state.
The [remaining-route inventory](migration_inventory.md#token-metadata-ownership-migration)
records the outstanding source/semantics decisions. Complete the declarations, then
remove `publish_limits` as a choice of authority. Unsupported routes need an explicit
value or pause/retirement decision; this PR makes none of those pauses automatically.
Do not create fake output limits for embedding/audio models.

Unlike response filtering, ordinary overrides also register these values in LiteLLM's
internal cost map. Aligning legacy `max_tokens` with the existing output override is
intentional; any internal reader of that alias now sees our value too. We do not enable
new prechecks, alter `modify_params`, or claim that all internal readers are inert.

## Direct-provider sources, 2026-10-05

The 33 direct chat routes (4 Anthropic, 2 Gemini, 25 Mistral, 2 Groq) now publish
Ducktape-owned input/output declarations and the derived legacy output alias. Values
live in `catalog.py`, with source links beside each group; no catalogue is imported at
generation time. The source choices are:

| Routes           | Source                                                                                                                                                                                         | Fields / scope                                                                                                                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direct Anthropic | [LiteLLM catalogue at `02f61c9c420b`](https://github.com/BerriAI/litellm/blob/02f61c9c420b9aa9de10ff673098ad7132b78f5b/model_prices_and_context_window.json)                                   | Exact unprefixed `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`, `claude-haiku-4-5-20251001` entries: `max_input_tokens` and `max_output_tokens`. Not Claude subscription or Tana limits. |
| Direct Gemini    | Google [3.7 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash) and [3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite), checked 2026-10-05 | Explicitly labelled **Input token limit** and **Output token limit**; both pages confirm the existing pair. Not Antigravity metadata.                                                           |
| Mistral chat     | [Same pinned LiteLLM catalogue](https://github.com/BerriAI/litellm/blob/02f61c9c420b9aa9de10ff673098ad7132b78f5b/model_prices_and_context_window.json)                                         | Exact `mistral/<model.id>` entries for all 25 declarations, including floating aliases and Voxtral's text-generating chat routes; copy the input/output fields, not just legacy `max_tokens`.   |
| Groq chat        | [LiteLLM 1.100.1 bundled backup at `1dba17b10ded`](https://github.com/BerriAI/litellm/blob/1dba17b10ded12ad0021edb453ba2c54e4637928/litellm/model_prices_and_context_window_backup.json)       | Exact `groq/llama-3.3-70b-versatile` and `groq/llama-3.1-8b-instant` input/output entries, verified against our pinned wheel's backup.                                                          |

Mistral's equal input/output bounds are **not additive**. Its
[chat API contract](https://docs.mistral.ai/api/endpoint/chat#operation-chat_completion_v1_chat_completions_post_request_max_tokens)
requires prompt tokens plus requested `max_tokens` to fit the model's context length.
We copy the catalogue's separate bounds, not a promise of a full-window prompt plus
full-window generation. No request generation cap or client compaction budget changes.
The remote snapshot differs from the bundled backup for `magistral-medium-latest`,
`magistral-small-latest`, and `mistral-medium`; these declarations deliberately adopt
the pinned remote entries. Floating aliases still require reviewed future refreshes.

The current remote catalogue omits the two Groq Llama entries; their selected source
is the pinned backup, not a guessed sibling model. Groq's documentation returned 403
during this audit. These are **last-known metadata declarations, not a fresh availability
or capacity verification**. No inference probes were run. Anthropic's direct-API pairs
are attached to separate model values; newer subscription declarations have their own
[source below](#claude-subscription-refresh-2026-10-09).
Embeddings, transcription, Antigravity, Ollama, Tana, and subscription-route source
choices remain outside this slice. Catalogue pricing/capabilities retain their existing
ownership, and ordinary token overrides still affect internal LiteLLM readers.

## Isolated config/API experiment, 2026-10-05

The missing runtime check is now complete for `/model/info` listing and
`?litellm_model_id=...`, using the **1.100.1 proxy-local dependency**. Eight fresh
processes exercised the real config loader and ASGI endpoint handlers, then the
catalogue fetch/install/replay path and same-router config reload. The
[temporary collector](https://github.com/agentydragon/ducktape/blob/cdada7566024793a350eff494597dfe97c4bd559/tana/litellm_proxy/test_proxy_runtime.py)
ran under `//tana/litellm_proxy:test_proxy_runtime`;
[CI passed](https://github.com/agentydragon/ducktape/actions/runs/37301188187), and
[BuildBuddy](https://app.buildbuddy.io/invocation/f504038a-972e-554a-a93c-0a01514077c0)
retains `publication-probe.json`. The collector is not permanent test plumbing.

The table shows **input / output / legacy** from `openai/gpt-4o-mini`. List and
single-deployment responses agreed for the selected fields in every case. `B` is
the bundled triple **128000 / 16384 / 16384**; `C` is a controlled initial catalogue
triple **900001 / 900002 / 900003**; `R` is the fetched replacement triple
**800001 / 800002 / 800003**; `P` is our explicit pair **111111 / 22222**.
The controlled values are deliberately synthetic, **not model capacity claims**.

| Initial catalogue / configured limits       | Initial API triple | After catalogue reload | After withdrawing overrides on same router |
| ------------------------------------------- | ------------------ | ---------------------- | ------------------------------------------ |
| Bundled / omitted or all null               | B                  | R                      | R                                          |
| Bundled / P                                 | P / 16384          | P / 800003             | P / 800003                                 |
| Controlled / omitted or all null            | C                  | R                      | R                                          |
| Controlled / P, with legacy omitted or null | P / 900003         | P / 800003             | P / 800003                                 |
| Controlled / all three zero                 | 0 / 0 / 0          | 0 / 0 / 0              | 0 / 0 / 0                                  |

This historical experiment shows that neither omission nor null suppresses catalogue
limits. Legacy-field removal is no longer a goal; a consistent explicit alias is acceptable.
A pair survives catalogue replacement while the legacy field continues following
that catalogue. Removing the pair from the file and calling
`ProxyConfig.load_config(previous_router, ...)` with stable deployment IDs did
**not** withdraw the published pair on this route. Do not generalize that result to
a fresh process or every DB/admin update path. Zero is a real registered value,
not an unknown sentinel. `context_window` was absent in all observed responses.

Adapter controls matter: `anthropic/gpt-4o-mini` and an unknown
`openai/publication-fixture:latest` returned null token fields without overrides,
not the OpenAI catalogue triple. Native Ollama discovery was **not validated**:
the fixture supplied a route-local `api_base`, but the metadata endpoint's
`get_litellm_model_info()` calls `get_model_info(model)` without forwarding it.
The source-traced `/api/show` behavior below must not be replaced with a claim
that native Ollama reliably leaves unknown limits unset.

Limit overrides left the matching OpenAI catalogue's prices and function-calling
flag intact. The pricing helper for 10 input / 5 output tokens returned
$0.0000015 / $0.000003 on the bundled fixture and $0.00017 / $0.000095 after
reload, independent of the limit overrides. Both `modify_params` and router
`enable_pre_call_checks` resolved to false. This verifies selected metadata and
helper behavior, **not end-to-end spend logging, budgets or inference**.

Scope limits: auth was replaced with a synthetic admin; no ASGI lifespan, database,
paid requests or production changes were involved. `/v2/model/info` returned 500
because no database was connected, so its publication behavior remains untested.
Reload exercised `refetch_model_cost_map()` plus `_swap_in_model_cost_map()`, not
the admin endpoint's authorization or cross-pod signaling. The ordinary-override policy
is described under [publication ownership](#publication-ownership).

## Token KVPs in the remote catalogue

These are catalogue defaults, **not our serving-path facts**. Values are tokens.
Other catalogue KVPs include mode, pricing, caching rates, and capability flags;
those need separate treatment, not deletion alongside limits.

| Matching model family / adapter                               | `max_input_tokens` | `max_output_tokens` | `max_tokens` |
| ------------------------------------------------------------- | -----------------: | ------------------: | -----------: |
| OpenAI GPT-6 Astra/Sol/Luna; GPT-5.6 Sol/Terra/Luna           |             922000 |              128000 |       128000 |
| OpenAI GPT-5.4 / GPT-5.5                                      |            1050000 |              128000 |       128000 |
| Anthropic Opus/Sonnet/Fable 5; Sonnet 4.6                     |            1000000 |              128000 |       128000 |
| Anthropic Haiku 4.5                                           |             200000 |               64000 |        64000 |
| Google Gemini 3.7 Flash / 3.5 Flash Lite                      |            1048576 |               65536 |        65536 |
| Google Gemini Embedding 2                                     |               8192 |              absent |         8192 |
| Google Gemini Embedding 001                                   |               2048 |              absent |         2048 |
| Mistral Codestral / code / code-FIM                           |             128000 |              128000 |       128000 |
| Mistral Magistral, Ministral 8B/14B, medium/small/vibe routes |             262144 |              262144 |       262144 |
| Mistral Ministral 3B                                          |             131072 |              131072 |       131072 |
| Mistral Voxtral Small                                         |              32768 |               32768 |        32768 |
| Groq Whisper                                                  |             absent |              absent |       absent |

The bundled catalogue differs: GPT-6 entries are absent; Magistral has all three
values at 40000; `mistral-medium` has input 32000, output/legacy 8191. Bundled Groq
Llama 3.3 70B has 131072/32768/32768, and Llama 3.1 8B has all three at 131072;
those exact entries are absent from the remote snapshot.

Additional cases:

- `openai/gpt-*` can use OpenAI entries. `anthropic/gpt-*` fails that provider match.
- Antigravity `anthropic/gemini-*` does not simply inherit Google entries; its
  `anthropic/claude-sonnet-4-6` can inherit Anthropic's raw-API entry.
- A Claude-family regex supplies input 200000, output 64000, legacy 64000 even
  without an exact entry. It matches our Tana Claude names and the Antigravity
  `claude-opus-4-6-thinking` name.
- Our exact local Ollama tags lack static entries. Native discovery can read recorded
  `context_length` from `/api/show` and put it into **all three** token-limit fields.
  That is not a measured output ceiling or necessarily the effective `num_ctx`.
- `context_window` is not a standard catalogue field used by this projection. Our
  former custom field should not be smuggled back in as a generic client budget.

## Observed deployed mixture

On 2026-10-05, `/model/info` under the cheap-experiments key returned:

| Luna route                         |  Input | Output | Legacy |
| ---------------------------------- | -----: | -----: | -----: |
| `chatgpt/ant-messages/gpt-6-luna`  | 372000 | 128000 |   null |
| `chatgpt/oai-responses/gpt-6-luna` | 372000 | 128000 | 128000 |

These responses contain existing overrides. They are not pristine catalogue
lookups, and the restricted key does not establish the state of every deployment.

Antigravity's source fields and serving-path checks have their own
[provider investigation](antigravity_limits.md); raw provider metadata is distinct
from LiteLLM's publication behavior.

## Who consumes these KVPs?

- `/model/info` clients and the LiteLLM dashboard see published metadata.
- LiteLLM itself can use the model-cost map, not only display it. In 1.100.1:
  - `get_modified_max_tokens()` is guarded by a supplied request `max_tokens`,
    `litellm.modify_params=True`, and a completion/acompletion/anthropic_messages
    call type. Responses is not in that guard.
  - Router context prechecks require `enable_pre_call_checks=True`.
  - The separate I/O token-rate limiter can fall back from `max_output_tokens` to
    `max_tokens` when estimating output reservations without a request cap; its
    deployment I/O limits must be configured for that path to run.

  Our generated LiteLLM config enables `drop_params`, not `modify_params` or router
  pre-call checks. No deployment I/O limits were found in the inspected generated
  config/key declarations; that is not an audit of every live DB/key setting.
  These guards do not establish that no other adapter or callback reads the map.
  **The later audit below found an unguarded Anthropic output-default reader.**

- OpenClaw's audited discovery reads `/v1/models` or `/models`, not `/model/info`.
  Its configured budgets come from our OpenClaw projection.
- Our Agentplane launch adapters do not obtain their context overrides from
  `/model/info`. Runners read `/agentplane/model-config` from Agentplane ingress,
  populated from `RUNNER_CONTEXT_OVERRIDES`, not `Model.limits`. The result is
  explicit client policy, not a LiteLLM catalogue lookup.
- Claude and Codex have their own model recognition/catalogues. Removing a LiteLLM
  metadata key does not remove a harness's built-in assumption.

Therefore response filtering and changing LiteLLM's internal catalogue are different
changes. Do not solve one by silently changing pricing or request behavior in the other.

## Gemini embedding input metadata (2026-10-09)

`EmbeddingLimits` declares only input tokens. The two Gemini declarations retain
ceilings recorded from Google's embedding documentation on 2026-07-30 and match the
exact `gemini/gemini-embedding-2` and `gemini/gemini-embedding-001` entries in
[LiteLLM v1.100.1's catalogue](https://github.com/BerriAI/litellm/blob/1dba17b10ded12ad0021edb453ba2c54e4637928/model_prices_and_context_window.json),
inspected 2026-10-09: **8192** and **2048** respectively. These are declared input
ceilings, not newly measured capacity or per-modality size/duration limits.

Those pinned entries use both `max_input_tokens` and legacy `max_tokens` for the
same input ceiling and have no `max_output_tokens`. The projection follows that
mode-specific convention, deriving both keys from one declaration. The generative
output-alias mapping above is unchanged. No vector dimension is reinterpreted as a
token limit, and no `null` suppression or downstream LiteLLM patch is introduced.

The durable bare `gemini-embedding-2` alias references its canonical route and
inherits the same metadata. This does not change upstream models, embedding spaces,
request-selected dimensions, indexes, prices, key selections or client budgets.
Audio metadata remains unresolved; `publish_limits` is still transitional.
This pins these input fields, not all catalogue capability metadata.

## Ollama embedding input metadata (2026-10-09)

For `qwen3-embedding:4b`, live Ollama 0.34.4 `/api/show` reported
`qwen3.context_length=40960`, `qwen3.embedding_length=2560`, EOS insertion enabled,
and no baked context/batch options. The installed GGUF blob SHA-256 was
`2b0cf8f17b4c723c27303015383c27ec4bf2d8314bb677d05e920dd70bb0f16b`.
The operator chose this **GGUF-declared 40960** for input metadata, rather than the
[Qwen model table's 32K](https://github.com/QwenLM/Qwen3-Embedding#model-overview)
or the deployment's global 131072 context allocation. Both `max_input_tokens` and
legacy input alias `max_tokens` derive from it; there is no output-token ceiling.

The [pinned LiteLLM adapter](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/llms/ollama/completion/handler.py)
forwards `truncate` to `/api/embed`. Ollama embedding routes now default to
`truncate: false`: [Ollama's handler](https://github.com/ollama/ollama/blob/v0.34.4/server/routes.go#L835-L1005)
rejects oversized input instead of truncating to context or retrying a runner
rejection with a shorter prefix. Model identity, dimensions, indexes and serving
allocations are unchanged. Clients that previously relied on silent truncation may
now receive errors.

**Not yet tested with live embedding requests.** The declaration is not proof that
40960 user-content tokens fit: special-token accounting and the runner's embedding
batch may impose lower limits (the pinned default batch is 2048). The TODO beside
the declaration tracks full-input/boundary verification. This change does not raise
`num_ctx`/`num_batch`, load the model, or authorize a capacity probe.

## Claude subscription refresh, 2026-10-09

The `anthropic-max20/ant-messages/*` roster now selects **Opus 5.5, Sonnet 5.5,
Fable 5.1 and Haiku 5.5**, replacing the older four subscription IDs. The existing
Claude key lane, paused Nix wrapper and parked OpenClaw aliases derive their selections
from those routes; no consumer is unpaused. Direct-API and Antigravity routes are unchanged.

[Gateway catalogue snapshot `e63af9856bda`](https://github.com/router-for-me/models/blob/e63af9856bda19828dfe93a6fa0559a5ab32965c/models.json)
declares `context_length = 1000000` and `max_completion_tokens = 128000` for each
exact new ID. Pinned CLIProxyAPI
[`7fac6b15bcfe`](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/registry/model_registry.go#L1451-L1476)
publishes these as `max_input_tokens` and `max_tokens`. We expose that declaration
through LiteLLM as input/output fields plus the derived legacy output alias.
These are shared gateway catalogue declarations, **not Max20 account measurements**
or a promise that both maxima fit together. LiteLLM's Anthropic discovery retains
model IDs only, so it does not import the backend limits automatically.

The pinned gateway's embedded catalogue predates the three 5.5 entries. Its
[updater](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/registry/model_updater.go)
can refresh from the upstream catalogue on startup and every three hours; runtime
configuration and fetch success still matter. The roster change did not upgrade the
gateway binary or change its updater.

[Read-only rollout verification, 2026-10-09 07:20–07:23 PDT](https://github.com/agentydragon/ducktape/issues/9574#issuecomment-6082872487)
confirmed all four exact IDs in the live gateway registration and proxy configuration,
the applied subscription key selections, and matching positive pricing metadata on
both LiteLLM replicas. This closes the registration/configuration/pricing-presence
checks for that dated rollout, not account request success or spend accounting.

TODO(#9574): account request success, beta-dependent input boundaries, full output
and joint capacity remain untested.
The executor forwards a requested 1M-context beta rather than enabling it from
metadata alone; the retained wrapper's `[1m]` convention is unchanged. It fills
an omitted request `max_tokens` from the registry, but that is not a general clamp
on caller-supplied values. No beta, request-cap, client-budget, pricing override,
reasoning-effort policy, inference probe or capacity test is added here.

The verified rollout used a current LiteLLM cost map: the pinned 1.100.1 bundled backup
omits these four IDs, while the inspected
[remote snapshot `63a4f3f2f333`](https://github.com/BerriAI/litellm/blob/63a4f3f2f3334bbd239c4c3e4c3302b63b02f71d/model_prices_and_context_window.json)
contains them. Pricing ownership remains with that catalogue; do not treat missing
runtime pricing as zero cost or assume accounting is verified by token metadata.

## Ollama GGUF context audit (2026-10-09)

A read-only audit of Ollama **0.34.4** used `/api/version`, `/api/tags` and
`/api/show`. The neutral source declarations in `ollama.py` now own the installed
GGUF `gguf_context_length` facts. The existing embedding input projection consumes
that fact instead of repeating its number; its published value is unchanged.

| Installed tag                          | GGUF context key          | Declared context | Baked `num_ctx` |
| -------------------------------------- | ------------------------- | ---------------: | --------------: |
| `qwen3.8-flash-next-iq4xs:latest`      | `qwen4exp.context_length` |           262144 |          131072 |
| `qwen3.8-flash-next-iq4xs-256k:latest` | `qwen4exp.context_length` |           262144 |          262144 |
| `gpt-oss:20b`                          | `gptoss.context_length`   |           131072 |          absent |
| `gpt-oss:120b`                         | `gptoss.context_length`   |           131072 |          absent |
| `gemma4:31b-it-q8_0`                   | `gemma4.context_length`   |           262144 |          absent |
| `qwen3-embedding:4b`                   | `qwen3.context_length`    |            40960 |          absent |

The Qwen variants share one source model declaration. These are **dated installed
artifact facts**, not guarantees about future pulls of mutable tags. `/api/tags`
reported these manifest SHA-256 digests (not GGUF blob digests):

```text
qwen3.8-flash-next-iq4xs:latest      828a9556d513518bd2ab4ea89d3a28673547258ac6ae223d108163bc35a80efb
qwen3.8-flash-next-iq4xs-256k:latest 14604e8e5a94e208a9ce10f148398914dec3c55c27741597c708339d22b6d753
gpt-oss:20b                        17052f91a42e97930aa6e28a6c6c06a983e6a58dbb00434885a0cf5313e376f7
gpt-oss:120b                       a951a23b46a1f6093dafee2ea481d634b4e31ac720a8a16f3f91e04f5a40ecd9
gemma4:31b-it-q8_0                 53dd8459790f8795177444daa9e33f417e03c0d1cdedb80b6c73898603d20aef
qwen3-embedding:4b                 df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907
```

### Why chat limits remain unpublished

[Ollama 0.34.4 runner creation](https://github.com/ollama/ollama/blob/b2da9e468af2479058ae18c6d908ed29de410684/llm/server.go#L98-L108)
clamps a requested `num_ctx` to a positive GGUF context length when the request
exceeds it. Thus the retained native GPT-OSS 20B 256K/512K/1M route labels are
**requested allocations, not evidence of those effective capacities**. The larger
OpenAI exposures remain parked; this audit does not change the retained native
routes or authorize restoring the OpenAI ones.

The source deployment's default context allocation is 131072; an absent baked
option does not establish a model's served capacity. The `/v1` path still cannot
select native `options.num_ctx`, while the Qwen 256K tag has a baked allocation.
Neither the allocation nor GGUF context supplies an independent chat output
ceiling, and context must accommodate prompt, generated tokens and overhead.
We do not manufacture a generative input/output pair from these numbers, introduce
request caps, or change the pair-or-none publication contract in `design.md`.

After removing the six GPT-5.6 ChatGPT entries, published coverage is **58/74 entries**
(54 generative pairs and four embedding input-only declarations). The 13 Ollama chat entries still have no
catalogue-owned token limits; two audio and one image entry also remain unresolved.
`publish_limits` is still transitional, not switched on by this source audit.

TODO(#9574): choose defensible chat publication semantics before projecting GGUF
context into client-facing limits. Full-input, output and joint-capacity boundaries
remain **untested**. No inference, model loading, allocation/provisioning changes or
capacity probes were performed. `/api/ps` before and after showed only the already
loaded Qwen 256K model; the shared service was not idle.

## Catalogue fallback and request defaults, 2026-10-10

Scope: source/catalogue inspection of the 74 served entries in the current roster,
not an isolated runtime test with overrides removed. No inference, deployment changes
or new account access occurred. The production map can differ due to fetch timing,
runtime registrations, cache state and adapter discovery.

Inspected LiteLLM **v1.100.1** lookup/registration code and its
[bundled catalogue](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/model_prices_and_context_window_backup.json),
plus remote catalogue snapshot
[`16fcbffa7fd344c611c627ef720961b4cdd1c211`](https://github.com/BerriAI/litellm/blob/16fcbffa7fd344c611c627ef720961b4cdd1c211/model_prices_and_context_window.json).
The public route slug does not isolate a route from catalogue lookup: the underlying
`litellm_params.model` supplies provider-prefixed and stripped-name candidates.

### Static matches and dynamic discovery without our overrides

These are fallback declarations, **not verified serving limits**. Values are input / output.

| Served family                                    | Remote snapshot fallback                                                                         | Bundled fallback / caveat                                                      |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| ChatGPT GPT-6 Responses, all three models        | `openai/gpt-6-*` matches 922000 / 128000                                                         | No GPT-6 entries                                                               |
| ChatGPT GPT-6 Messages                           | `anthropic/gpt-6-*` fails the provider-aware OpenAI entry match                                  | No GPT-6 entries; separate `get_max_tokens` lookup is not provider-equivalent  |
| Claude subscription, all four current models     | Exact entries: 1000000 / 128000                                                                  | Generic Claude rule: 200000 / 64000                                            |
| Direct Claude Opus/Sonnet/Fable 5; Haiku 4.5     | 1000000 / 128000; Haiku 200000 / 64000                                                           | Same pairs                                                                     |
| Antigravity Claude Sonnet 4.6; Opus 4.6 thinking | Exact Sonnet 1000000 / 128000; regex Opus 200000 / 64000                                         | Same; not account-specific evidence                                            |
| Antigravity Gemini/GPT-OSS names                 | No compatible static token-limit match; some capability rules have no token fields               | No compatible static token-limit match                                         |
| Direct Gemini chat                               | 1048576 / 65536                                                                                  | Same                                                                           |
| Gemini embeddings, including bare alias          | 8192 or 2048 input; legacy `max_tokens` repeats input                                            | Same; no output ceiling                                                        |
| Mistral, all 25 routes                           | Exact matches; mostly 262144 / 262144, Codestral/code 128000, Ministral 3B 131072, Voxtral 32768 | Magistral instead 40000 / 40000; `mistral-medium` 32000 / 8191                 |
| Groq Llama chat                                  | No exact current matches                                                                         | Llama 3.3: 131072 / 32768; Llama 3.1: 131072 / 131072                          |
| Groq Whisper                                     | Entries have no token-limit fields                                                               | Same                                                                           |
| Native Ollama chat/embedding                     | Adapter may query `/api/show`, copying `context_length` to input, output and legacy token fields | Not disabled by selecting the bundled map; not necessarily effective `num_ctx` |
| OpenAI-compatible Ollama custom tags             | No compatible static token-limit matches found                                                   | Same                                                                           |

See [`_get_model_info_helper`](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/utils.py#L5594-L5803)
and [native Ollama discovery](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/llms/ollama/common_utils.py#L162-L231).
Exact matching, provider checks, regex rules, runtime registration and dynamic lookup
are distinct paths; this table is not a promise of identical final endpoint responses.

### Request-affecting behavior: correction to the descriptive-only interpretation

The [Anthropic chat adapter](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/llms/anthropic/chat/transformation.py#L300-L323)
sets an omitted `max_tokens` using `get_max_tokens(model)`, falling back to
`DEFAULT_ANTHROPIC_CHAT_MAX_TOKENS` (4096 unless configured). Its
[request transformation](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/llms/anthropic/chat/transformation.py#L1939-L1946)
inserts those defaults only for parameters absent from `optional_params`.
**This path does not require `modify_params=True`.** An explicit caller output cap
wins over this default. This finding covers chat-completions-to-Anthropic translation;
do not automatically generalize it to native Messages or Responses passthrough.

[`get_max_tokens`](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/utils.py#L5149-L5215)
can use a bare model entry after provider-prefix removal and is not the same as the
provider-aware metadata lookup. Thus failure to match `anthropic/gpt-6-*` metadata
is not proof against a catalogue-derived output default. Registration order can also
influence which map entry a later reader sees.

The backend normally enforces a forwarded output budget: it may finish naturally,
terminate at that budget with a length/max-tokens finish reason, or reject an invalid
budget/context combination. This is not automatic input truncation. Separately,
router prechecks can exclude deployments before dispatch; supported `modify_params`
paths can reduce output allowances. Our generated config enables neither mechanism,
but that does not disable the adapter default above. On our subscription Responses
path, CLIProxyAPI strips output caps altogether, so a requested cap is not a reliable
bound there ([pinned source and measurements](client_budgets.md#luna-subscription-path-probes-2026-10-10)).

### Configuration implications and open decision

Removing Ducktape's token projection restores fallback; YAML null is not reliable
suppression. `LITELLM_LOCAL_MODEL_COST_MAP=True` selects the bundled catalogue, not an
empty map. The [loader](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/litellm_core_utils/get_model_cost_map.py)
rejects an empty remote map and falls back to the bundle. The same catalogue carries
pricing, capabilities and provider information, so removing it wholesale is not a
safe token-only change.

No production Ducktape client reading LiteLLM token metadata to set its context
budget was found in the repository audit. Agentplane's ingress policy, Nix wrapper
budgets and OpenClaw configuration are separate readers/settings. This does **not**
make LiteLLM token fields inert: the adapter default above is a concrete counterexample.

No suitable token-only disable switch was established in the inspected paths; we have
**not proved universal catalogue disablement impossible**. Keep existing runtime behavior
while evaluating the [routing contract and alternatives](routing_review.md), rather than
expanding metadata ownership or deleting the catalogue on that assumption.
