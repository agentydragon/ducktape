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

**Agreed direction:** legacy and current fields may coexist. The requirement is that
related token values come from one consistent source, not that legacy keys disappear.
**Every served route's token metadata will be declared by Ducktape.** It is acceptable
to copy values from a specific LiteLLM catalogue entry, with revision/source comments
beside the declaration; the catalogue is a reference input, not an implicit runtime
second authority. We do not need independent live capacity probes for every model.

For routes marked `publish_limits`, the ordinary cdk8s projection emits:

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
are attached to separate model values, leaving subscription declarations unknown.
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

- OpenClaw's audited discovery reads `/v1/models` or `/models`, not `/model/info`.
  Its configured budgets come from our OpenClaw projection.
- Our Agentplane launch adapters do not obtain their context overrides from
  `/model/info`. They read runner-owned configuration.
- Claude and Codex have their own model recognition/catalogues. Removing a LiteLLM
  metadata key does not remove a harness's built-in assumption.

Therefore response filtering and changing LiteLLM's internal catalogue are different
changes. Do not solve one by silently changing pricing or request behavior in the other.
