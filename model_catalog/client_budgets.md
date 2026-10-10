# Client-specific budgets and investigations

Supporting evidence for the [model-roster design](design.md), using its
[shared token vocabulary](design.md#token-limit-vocabulary). Observations are dated
and version-scoped; they do not authorize configuration or client-budget changes.

Numbers below are preserved configuration choices at draft #9034 head `26862c4395`,
not newly validated provider limits and not a claim that the draft is deployed.

The draft retains a provider limit pair only for the direct Google Gemini models:
1048576 input / 65536 output, based on provider documentation. Historical GPT-5.6
subscription probing accepted 370629 input tokens and rejected 372194; it did not
establish an output ceiling or a combined capacity. The 372000 client budget is
not that complete contract. Astra's 872000 came from Codex client metadata; Sol/Luna
inherited earlier budgets rather than independent probes. Antigravity's upstream
`maxTokens` interpretation was unresolved in that draft; the [Antigravity investigation](antigravity_limits.md) now records
input-limit evidence for its 3.1 Flash Lite route, without extending that finding
to every Antigravity model. Client choices must not silently become provider facts,
nor should unsupported facts quietly fall back to raw-API catalogue metadata.

## Claude Code wrappers

`model_catalog/nix.py` → `claude-wrappers.json` →
`nix/home/claude_code/gateway.nix` maps:

- `maxContextTokens` → `CLAUDE_CODE_MAX_CONTEXT_TOKENS`.
- `maxOutputTokens` → `CLAUDE_CODE_MAX_OUTPUT_TOKENS`.

| Wrapper                                           | Context setting | Output setting |
| ------------------------------------------------- | --------------: | -------------: |
| `codex-claude` (Astra primary / Luna small model) |          872000 |         128000 |
| `gemini-claude`                                   |         1048576 |          65536 |
| `antigravity-claude`                              |         1048576 |          65536 |
| `litellm-claude`                                  |         omitted |        omitted |

`tana-claude` has no active generated selection while Tana exposure is parked
(#9574); its renderer and credentials are retained.

In the inspected Nix Claude Code **2.1.283**, the context override is an assumed
pre-reserve window for custom models; recognized model metadata can take precedence.
The normal observed compaction calculation subtracts an output reserve capped at
20000, then 13000 headroom. Other overrides and execution modes can alter this.
It is neither a plain max-input ceiling nor a universal backend context capacity.

`litellm-claude` also uses a `[1m]` name suffix as a **Claude client convention**.
The suffix is stripped before sending the model name upstream. Switching models
through gateway discovery can lose that convention. Do not create duplicate served
routes merely to encode a client's suffix convention.

Claude Code Web and direct local use must not inherit these gateway overrides just
because they use the same model family. Also, the Agentplane harness-test archive
is **2.1.252**, not 2.1.283: recheck the actual launched package before relying on
version-specific formulas.

## OpenClaw

Audited version: **2026.9.5**, source revision
`ec9c1a13db8938e5a3eaa51fca2e981cde2395a9`.
`contextWindow` is the pre-reserve client context budget; runtime `contextTokens`
can narrow it. Compaction uses `contextWindow - reserveTokens`, with additional
history/pending-input accounting. The core default reserve is 16384; agent policy
raises the floor to 20000, subject to a 25% window cap. It does **not** generally
reserve the full `model.maxTokens`. `maxTokens` is separate output/request metadata.

The public-coder projection currently preserves:

| Selection                                  | `contextWindow` | `maxTokens` |
| ------------------------------------------ | --------------: | ----------: |
| GPT-6 Astra                                |          872000 |      128000 |
| GPT-6 Sol / Luna                           |          372000 |      128000 |
| Direct Google Gemini routes                |         1048576 |       65536 |
| Antigravity Claude Opus / Sonnet           |          200000 |       64000 |
| Antigravity Flash group                    |         1048576 |       65536 |
| Antigravity Pro / Pro Low / Flash Lite 3.1 |         1048576 |       65535 |
| Antigravity GPT-OSS 120B Medium            |          114000 |       32768 |

These remain preserved client choices. Google's fresh metadata also distinguishes
65535/65536 output values, while now reporting 131072 rather than 114000 for GPT-OSS
([fresh Google metadata](antigravity_limits.md#fresh-google-metadata-2026-10-05-1201-utc)). The old 114000 came from the upstream snapshot and is preserved here
as an OpenClaw budget; neither use establishes both a provider input ceiling and a
combined window. Refreshing metadata does not silently update this client policy. Public Coder is now paused ([approved pauses](design.md#approved-pauses-and-remaining-decisions)); do not validate its entire matrix as a prerequisite
for fixing the active Codex path.

## Codex and Agentplane

Agentplane's ingress owns per-model client configuration. Each environment projects
its selected routes and explicit `RUNNER_CONTEXT_OVERRIDES` into ingress settings;
the runner looks up the selected model through authenticated
`GET /agentplane/model-config?model=...`. The shared `ModelConfig` record carries
`total_context_budget_tokens`, an input-plus-output client budget, not provider
capacity. The resolved budget is persisted with the session and used when resuming
its harness. See the [lookup contract](../agentplane/llm_ingress/README.md#per-model-client-configuration)
for no-override and failure behavior.

The cluster override selection is only Qwen IQ4_XS 128K/256K, both wires: 131072 and
262144 respectively. The active GPT subscription routes do **not** receive 872000
or 372000 merely because a wrapper or OpenClaw uses those values.

- Claude adapter sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when an override exists.
- Codex adapter passes `model_context_window` in its startup configuration. Our
  launch helper leaves auto-compaction at Codex's derived default; its comment
  records 90%. Codex also has its own effective-window and metadata rules: do not
  treat that comment as an end-to-end validated threshold for every current binary.
- Changing a model to one with a different configured window, including an absent
  versus present override, requires a new thread. The runner rejects that live switch.
- Harness telemetry is another output: Codex reports
  `thread/tokenUsage/updated.tokenUsage.modelContextWindow`; Claude result frames
  report `modelUsage[*].contextWindow`. Acceptance probes record these as harness
  observations, not backend capacity measurements.

## Active Codex path audit, 2026-10-05

This is a deployment observation, not a new neutral-roster contract. Read-only checks
at approximately 05:34–05:40 UTC examined this audit's **running Agentplane session**,
its native state/usage, deployed LiteLLM configuration, and ready proxy Pods. This
proves one active session, not every agent's version or model selection.

```text
Agentplane runner → Codex app-server 0.157.0
  → central egress → Agentplane LLM ingress /v1/responses
  → shared LiteLLM → CLIProxyAPI /v1/responses → Codex subscription backend
```

The ingress authenticates/replaces credentials and streams the request body unchanged;
it does not currently translate route names or inject context budgets. This pass-through
is an observation, not a required architecture: the operator explicitly permits model
translation at this boundary when useful for native harness behavior. Neither it nor
the runner reads LiteLLM `/model/info` to configure Codex.

| Layer                     | Observed configuration / behavior                                                                                                   | What that means                                                                               |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Native process and thread | Codex **0.157.0**, provider `agentplane`, model `chatgpt/oai-responses/gpt-6-astra`                                                 | A real launched binary/thread, not the native-test archive                                    |
| Launch configuration      | Responses wire to `http://agentplane-llm-ingress.agentplane-staging.svc.cluster.local:8080/v1`; no context/auto-compaction override | GPT budgets are not wired from the roster to this client                                      |
| Recognition               | Native log explicitly warns that the full Astra route uses fallback metadata                                                        | The hidden bare `gpt-6-astra` alias exists in LiteLLM but this session does **not** select it |
| Native telemetry          | Latest sampled `token_count.info.model_context_window`: **258400**; normal tool turns work                                          | Resolved client accounting, not a measured backend limit                                      |
| Deployed proxy config     | Astra: **872000 input / 128000 output**; Sol/Luna: **372000 / 128000**                                                              | Current explicit metadata overrides, with the mixed provenance described above                |
| Live Luna `/model/info`   | Both wires: **372000 / 128000**; legacy `max_tokens`: Responses **128000**, Messages **null**; custom `context_window` absent       | Removing our legacy/custom assignments did not eliminate adapter-dependent fallback           |

Live metadata was read with the substituted **cheap-experiments** key; its view includes
Luna, not Astra/Sol. The latter numbers above are from the deployed ConfigMap, not an
assertion about their `/model/info` responses. Ready LiteLLM Pods ran
`tana-litellm-proxy:devel-20261005030920-bde5d31` (that build pins **1.100.1**).
The deployment does not set `LITELLM_LOCAL_MODEL_COST_MAP`, so its package pin still
does not pin the downloaded catalogue. The ready CLIProxyAPI Pod ran
`cli-proxy-api:devel-20260924021629-ce93c9e`, whose source pin is `7fac6b15bcfe`.

### What Codex 0.157.0 does with these values

The [matching source](https://github.com/openai/codex/tree/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs)
corroborates the live fallback warning and telemetry:

- `models-manager/src/manager.rs` strips **one** namespace segment for recognition;
  `chatgpt/oai-responses/gpt-6-astra` has two and does not match the bundled Astra entry.
- `model-provider/src/models_endpoint.rs` does not enable API-key catalogue discovery
  for this custom provider without `model_catalog_url`. We do not configure one.
  LiteLLM's metadata fields therefore cannot repair this recognition failure.
- `models-manager/src/model_info.rs` gives fallback models context and override maximum
  **272000**, with **95%** usable: **258400**. `model_context_window` is clamped to the
  recognized entry's maximum; setting 872000 on the unrecognized route would not work.
- `protocol/src/openai_models.rs` derives the default automatic-compaction threshold
  from **90% of the resolved window**: **244800** here, not 90% of 258400. This is a
  source-derived default, not a newly measured compaction boundary or an output reserve
  of 128000. The usable-window headroom covers prompts, tools, and output together.
- Bundled Astra, Sol and Luna entries all default to **272000**, with override maximum
  **872000**. Merely switching to a recognized alias would **not** raise the default
  window. The maximum is a client override bound, not a subscription capacity fact.
- Recognition also changes prompt/reasoning defaults, freeform `apply_patch`,
  `tool_mode=code_mode_only`, and `use_responses_lite`. The tool mode has precedence over
  feature flags when code mode is available. Do not treat an alias as a context-only fix.
- **Version correction to the older audit:** 0.157.0 gates WebSockets on the provider's
  `supports_websockets` (default false), which our provider does not enable. Its JSON
  catalogue still contains `prefer_websockets`, but `ModelInfo` no longer consumes that
  field. Recognition alone does not enable WebSockets in this version.

See `core/src/tools/mod.rs`, `core/src/client.rs`, and
`model-provider-info/src/lib.rs` for those non-context effects. The older
[harness audit](debug/harness_model_metadata.md) remains historical evidence,
not a substitute for inspecting the launched version.

### Output limits are not enforced by these metadata fields

The current native `ResponsesApiRequest` (`codex-api/src/common.rs`) has no
`max_output_tokens` member. Our runner does not copy the roster's 128000 output value
into requests. Shell-tool `max_output_tokens` controls tool-output truncation, not
model generation length.

More importantly, the deployed gateway's pinned
[Responses translator](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe/internal/translator/codex/openai/responses/codex_openai-responses_request.go)
**deletes `max_output_tokens` and `max_completion_tokens`**, documenting that the
subscription backend rejects those request fields. Its executor defaults to
`https://chatgpt.com/backend-api/codex/responses`. This is source evidence for the
pinned gateway, not a capture of upstream request bodies or private configuration.

One tiny live Luna Responses request asking for `OK` succeeded: **307 input / 5 output
tokens**, status `completed`, response `max_output_tokens: null`. The request supplied
256, but this path must **not** be treated as enforcing that cap. The probe establishes
connectivity only, not input/output ceilings, long-context quality, or arbitrary-client
compatibility. No capacity stress test, alias launch, or live setting change was made.

### Consequence for the next change

Keep the working Codex launch behavior unchanged while fixing publication. The active
client's 258400 telemetry and LiteLLM's 872000/372000 metadata are separate outputs;
changing one does not update the other. Do not promote either, or the historical
128000 output value, into a justified subscription input/output pair.

The [LiteLLM investigation](litellm_metadata.md) records the pinned-LiteLLM publication experiment and request-side
consumer guards. The next step in the [rollout plan](design.md#proposed-shape-and-rollout) is selecting and validating a publication
mechanism, not another capacity audit. If changing Codex recognition is later
necessary, test the exact ingress-translation, alias or catalogue strategy with native tools, reasoning,
Responses-lite and compaction before enabling it. Do not build a general harness
metadata service to preserve paused consumers. Separate Claude/Codex configuration
vocabularies only when both actually need supported overrides.

## Subscription-path public evidence, 2026-10-05

**A Responses-shaped endpoint does not establish the public API's capacity contract.**
Public research supports that distinction but does not establish our account's backend
ceilings. Sources below are ranked by what they actually establish, not combined into
one supposedly authoritative number.

### Official API specifications and first-party Codex guidance

The public API model pages for
[Astra](https://developers.openai.com/api/docs/models/gpt-6-astra.md),
[Sol](https://developers.openai.com/api/docs/models/gpt-6-sol.md), and
[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna.md) all list **1050000
combined context, 922000 maximum input, and 128000 maximum output**. Those are API
specifications, not an explicit entitlement for the ChatGPT subscription backend.
The API's 272K long-context pricing threshold is not its capacity ceiling either.

There is also contrary evidence to a blanket claim that subscription Codex cannot use
1M: [Tibo (Codex & ChatGPT at OpenAI), 2026-08-16](https://x.com/thsottiaux/status/2089082893804896524)
explicitly recommends `model_context_window=1000000` and
`model_auto_compact_token_limit=900000` for **GPT-5.6 Sol**, describing the smaller
default as tuned for performance/cost. This establishes advertised intent for that
model, not verified acceptance on our GPT-6 routes. Codex's
[override-bound change](https://github.com/openai/codex/pull/39102) and the inspected
0.157.0 source instead use **872000** as the relevant maximum client override.
Do not silently resolve this discrepancy in favor of either advertised number.

### Gateway-maintainer account of the subscription backend

[CLIProxyAPI collaborator luispater, 2026-09-15](https://github.com/router-for-me/CLIProxyAPI/issues/5835#issuecomment-5672721746)
says Astra's actual limit is **1M total: 872000 context + 128000 output**, and calls
the catalogue's `context_length: 272000` OpenAI's recommended display value. This is
a useful subscription-specific claim, but **not an OpenAI serving contract or our own
boundary measurement**. Preserve the source's word “context”: do not quietly recast
it as a proven independently attainable maximum input. “Display only” also cannot
be generalized to our client: Codex demonstrably uses metadata to constrain budgets.

The [CLIProxy public registry snapshot `18a3f4b749db`](https://github.com/router-for-me/models/blob/18a3f4b749dbba28f6d7d81ef5c34caecb03d0d8/models.json),
rechecked 2026-10-10 after removing GPT-5.6, declares **272000 `context_length` and
128000 `max_completion_tokens`** for Astra/Sol/Luna in `codex-team`, `codex-plus`
and `codex-pro`; `codex-free` includes Luna with the same numbers. This is shared
catalogue metadata, not an account entitlement or a measured capacity pair.

The repo-pinned [CLIProxyAPI projection](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/registry/model_registry.go#L1409-L1476)
exposes `ContextLength` as `context_length` for OpenAI discovery, but renames the
same value to `max_input_tokens` for Claude discovery; `MaxCompletionTokens`
becomes `max_completion_tokens` or `max_tokens`, respectively. That adapter rename
is **not independent evidence of a subscription input ceiling**. The registry
calls `ContextLength` a context window, and this snapshot supplies neither a
separate maximum-input field nor an explicit combined-context contract. Do not
sum the two values or subtract output from context to manufacture one.

The [updater](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/registry/model_updater.go#L17-L93)
loads an embedded fallback and refreshes remotely on startup/every three hours.
The inspected source and public snapshot do not prove the effective account
catalogue of the running gateway. Its pinned [Responses translator](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/translator/codex/openai/responses/codex_openai-responses_request.go#L24-L34)
also removes `max_output_tokens` and `max_completion_tokens` before forwarding;
the 128000 catalogue declaration is not evidence of request-cap enforcement.

### Reproductions and historical reports, not provider guarantees

- **Client clamping:** an [Astra ChatGPT-sign-in reproduction](https://github.com/openai/codex/issues/41325#issuecomment-5746583842)
  and a [Sol report](https://github.com/openai/codex/issues/47805) show default 272000,
  override maximum 872000, and 95% usable. Requesting 1000000/1050000 yields **828400**
  reported usable tokens; requesting 800000 yields 760000 in the Sol report. These
  corroborate client behavior, **not backend rejection above 872000**.
- **Catalogue variability:** [#40258](https://github.com/openai/codex/issues/40258)
  reports different Sol caps for the same account depending on client-identification
  headers; the reporter says that incident resolved on 2026-09-02.
  [#39144](https://github.com/openai/codex/issues/39144) reports account differences.
  Neither proves a current incident, and header spoofing is not our proposed fix.
- **Historical 372000:** gateway-maintainer explanations in
  [#4195](https://github.com/router-for-me/CLIProxyAPI/issues/4195#issuecomment-4937617812)
  and [#4476](https://github.com/router-for-me/CLIProxyAPI/issues/4476#issuecomment-5034688225)
  concern GPT-5.6 catalogue values and acceptance beyond its smaller advertised
  budget. They do not establish GPT-6 Sol/Luna input ceilings.
- **Inference lower bound:** [#43685](https://github.com/openai/codex/issues/43685)
  reports successful Astra subscription-backed requests with **260307 and 300307
  prompt tokens**. This is third-party evidence of acceptance, not a measured maximum
  or proof of long-context attention quality. Its reported short output cap does not
  establish cap enforcement through our pinned translator.

### Decision and remaining evidence

There is no justified universal subscription input/output pair to install from these
sources. In particular, do not copy the API's 922000/128000, promote our historical
372000/128000, or subtract 128000 from a native client budget to invent a ceiling.
The Astra maintainer claim is a candidate contract requiring corroboration; it is not
permission to extend it to Sol/Luna. The 2026-10-10 recheck therefore leaves the six
retained GPT-6 route declarations provisional: Astra 872000/128000, Sol/Luna 372000/128000. No newly justified
input/output pair was found. Keep published metadata and consumer budgets unchanged;
the unresolved publication decision is not another automatic source-audit task.

A next non-inference check would be a sanitized catalogue for the actual upstream
account and client path, through an explicitly authorized credential-substitution
route; that still establishes only advertised metadata. The public-internet grant
does not grant upstream account credentials. Any backend boundary probes require a
separate quota/cost budget and authorization: the deployed gateway strips output caps,
so requesting a tiny output is not a reliable cost bound. No near-limit inference,
credential-file inspection, or header-spoofing experiment was performed for this research.

## Opt-in long-context experiment and costs

The operator prefers a **separate experimental Agentplane model option**, then a real
working session allowed to accumulate context, over a synthetic near-limit probe suite.
Keep the ordinary option/default intact. This is a client launch preset for the same
backend model, not a new provider model or evidence for different neutral-roster limits.
Reuse the existing offering/configuration machinery where possible; do not build another
registry or add a LiteLLM route unless the actual selection/recognition wiring requires it.
This section records the plan, not an enabled option.

**Model identity need not pass through unchanged.** Agentplane's selected offering,
the harness-facing native model ID, and the LiteLLM routing ID serve different purposes.
The operator explicitly permits changing ingress translation rather than contorting
LiteLLM aliases or requiring native clients to recognize our routing namespace. One
candidate, subject to a native-client smoke test, is:

```text
Agentplane option: Astra / Astra — long context (experimental)
  → Codex model: gpt-6-astra, with the option's client budget
  → Agentplane ingress translates the model to: chatgpt/oai-responses/gpt-6-astra
  → existing LiteLLM route and subscription backend
```

Both options can share that native identity and backend route; only their launch
budget differs. Keep this mapping with the existing Agentplane offering/launch
configuration, not in a new registry or a provider-capacity declaration. Route selection
must remain authorized and unambiguous if multiple offerings use the same native slug;
a caller-supplied native name must not bypass the selected route's access controls.
Check response model identifiers, model switching, resume and telemetry as well as
request rewriting. Native recognition still has the prompt/tool side effects noted
above; translation is not automatically a context-only change. The same boundary is
available for future Claude support, without implementing that paused path now.

- Give the option an honest label such as “Astra — long context (experimental)”, not
  “1M guaranteed”. Start a fresh native thread; do not mutate an existing thread's budget.
- Resolve model recognition explicitly, including the tool/prompt behavior described
  above. Merely requesting 1000000 on our current unrecognized route stays clamped.
  The inspected recognized-model maximum is **872000 configured / 828400 usable**;
  Tibo's 1000000 recommendation concerns a different model/version context and does not
  override that clamp. Recheck the exact launch version before choosing the preset.
- Record the effective budget and compaction threshold at startup. With the inspected
  default 90% policy, an 872000 window starts automatic compaction at **784800**.
  Normal use may therefore encounter compaction before a backend limit; that is useful
  operational evidence, but not a maximum-capacity test. Raising/disabling compaction
  would be a separate deliberate experiment, not an incidental preset change.
- During ordinary work, record input/cached-input/output and reasoning usage when
  available, latency, quota before/after, compaction events, and errors. Distinguish a
  native-client clamp, context rejection, quota exhaustion, proxy timeout, and degraded
  recall. Stop on trouble rather than repeatedly retrying a large failing request.

### How expensive is a long-context probe?

There are **three different ledgers**: upstream included subscription allowance or
purchased credits, hypothetical direct-API spend, and LiteLLM's local spend accounting.
None is automatically a conversion formula for the others. These examples use the
public rates retrieved **2026-10-05**, Standard speed, text-only, and one inference
request. A user-visible agent turn can contain many requests; retry and compaction
requests also contribute usage. Increasing the configured window alone sends no tokens.

**On our subscription path**, included usage is not billed as an equivalent raw-API
request. The [Codex pricing page](https://learn.chatgpt.com/docs/pricing#token-rates)
explicitly says neither API prices nor credit prices determine included allowance
consumption. It depends on context, reasoning, tools, caching and other factors. We
cannot honestly predict “this probe consumes X% of your plan” from prompt size alone.
Purchased-credit rates do allow a conditional estimate:

| Model       | Credits / 1M uncached input | Credits / 1M cached input | Credits / 1M output | 800K uncached input + 1K output |
| ----------- | --------------------------: | ------------------------: | ------------------: | ------------------------------: |
| GPT-6 Astra |                         250 |                        25 |                1250 |                  201.25 credits |
| GPT-6 Sol   |                          50 |                         5 |                 250 |                   40.25 credits |
| GPT-6 Luna  |                         2.5 |                      0.25 |                12.5 |                  2.0125 credits |

These are the published Standard credit rates, not a claim that our account is using
purchased credits. The page has no separate cache-write charge for Codex credits and
warns about legacy Enterprise rate cards. Do not import the raw API's long-context
multiplier into this credit calculation. Credit purchase prices depend on the plan or
agreement; actual included-quota impact needs before/after observations on the account
that served the request, accounting for concurrent activity.

**As a paid-API comparison only**, the model pages linked above apply 2× input/cache
rates and 1.5× output rates to the **whole request** above 272K input tokens:

| Model       | 300K cold input | 800K cold input | 872K cold input | 800K fully cache-hit input | Additional 1K output |
| ----------- | --------------: | --------------: | --------------: | -------------------------: | -------------------: |
| GPT-6 Astra |           $6.00 |          $16.00 |          $17.44 |                      $1.60 |               $0.075 |
| GPT-6 Sol   |           $1.20 |           $3.20 |          $3.488 |                      $0.32 |               $0.015 |
| GPT-6 Luna  |           $0.06 |           $0.16 |         $0.1744 |                     $0.016 |             $0.00075 |

Cold-input examples use ordinary uncached-input prices, excluding any separately
charged cache writes, tools, images, regional/speed premiums or tax. Fully cache-hit
figures are an idealized comparison, not a promise of cache eligibility or retention.
Billable output includes applicable reasoning tokens, not just visible response text.
For example, 800K cold input plus 1K output is **$16.075 Astra / $3.215 Sol /
$0.16075 Luna** on that API rate card. A successful Luna test would not prove Astra's
capacity. Ten independent cold 800K Astra requests cost **$160 input alone**; one cold
plus nine fully cache-hit requests would instead cost **$30.40 input**, before any
other charges. One probe and a multi-step session are very different budgets.

Our gateway strips request output caps ([translator evidence](#output-limits-are-not-enforced-by-these-metadata-fields)), so “reply OK” is an instruction, not a
hard cost bound. At the API comparison rates, 128K output would add $9.60/$1.92/$0.096
respectively; this arithmetic does not verify a subscription output ceiling. The
practical concern is shared subscription quota and repeated long turns, not that every
single probe must be prohibitively expensive. No long-context request was sent for
these estimates, and no experimental option has yet been enabled.
