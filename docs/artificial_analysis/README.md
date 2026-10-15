# Artificial Analysis — selected model snapshots

Source and attribution: [Artificial Analysis](https://artificialanalysis.ai/).
These are selected comparison rows, not a mirror of AA's full dataset. Retain that
scope when refreshing; API access does not itself grant redistribution rights.

## Current snapshot: 2026-10-14

<cited_models_2026_10_14.csv> contains the previous 57 comparison rows refreshed
from the [leaderboard](https://artificialanalysis.ai/leaderboards/models), plus
13 rugged candidate/effort rows (70 total). Historical CSVs remain unchanged.
The [methodology page](https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index)
identifies the index as **v4.3.2** on this retrieval date.

Unlike September, this snapshot retains **estimated indices** so small local
candidates are represented. Filter `intelligence_index_is_estimated == false`
for measured-index comparisons. An estimated index does not make every component
score estimated. Missing fields stay blank; neither blanks nor absent models mean
zero. Where the public response omits `intelligenceIndexCostPerTask`, costs/task are
blank rather than carried forward.

### Rugged candidates among the comparison models

Scores below are AA's external results, not measurements of our quantized weights,
KV caches, harness, or available context. Reasoning variants are separate rows;
our current router profiles disable reasoning. Gemma QAT and the two E2B context
profiles map to the same named base model, not separately evaluated AA models.
Context in the CSV is AA's advertised context, not a successful rugged input size.

| AA model / setting                                                                                | Index | Estimated index? | GPQA Diamond | AA-LCR |
| ------------------------------------------------------------------------------------------------- | ----: | ---------------- | -----------: | -----: |
| [Qwen3 4B 2507 Instruct](https://artificialanalysis.ai/models/qwen3-4b-2507-instruct)             |  6.74 | yes              |       51.72% | 11.33% |
| [Gemma 4 E2B (Non-reasoning)](https://artificialanalysis.ai/models/gemma-4-e2b-non-reasoning)     |  6.48 | yes              |       40.51% | 16.33% |
| [Gemma 4 E2B (Reasoning)](https://artificialanalysis.ai/models/gemma-4-e2b)                       |  7.77 | yes              |       43.33% | 16.33% |
| [Gemma 4 E4B (Non-reasoning)](https://artificialanalysis.ai/models/gemma-4-e4b-non-reasoning)     |  7.46 | yes              |       54.95% | 24.00% |
| [Gemma 4 E4B (Reasoning)](https://artificialanalysis.ai/models/gemma-4-e4b)                       |  8.91 | no               |       57.58% | 32.00% |
| [Qwen2.5 Coder Instruct 7B](https://artificialanalysis.ai/models/qwen2-5-coder-7b-instruct)       |  5.79 | yes              |       33.94% |      — |
| [Qwen3 8B (Non-reasoning)](https://artificialanalysis.ai/models/qwen3-8b-instruct)                |  5.99 | yes              |       45.15% |  0.00% |
| [Qwen3 8B (Reasoning)](https://artificialanalysis.ai/models/qwen3-8b-instruct-reasoning)          |  5.16 | no               |       58.89% |  0.00% |
| [Qwen3.5 0.8B (Non-reasoning)](https://artificialanalysis.ai/models/qwen3-5-0-8b-non-reasoning)   |  5.40 | yes              |       23.64% |  8.00% |
| [Qwen3.5 0.8B (Reasoning)](https://artificialanalysis.ai/models/qwen3-5-0-8b)                     |  6.13 | yes              |       11.11% |  9.00% |
| [Qwen3.5 4B (Non-reasoning)](https://artificialanalysis.ai/models/qwen3-5-4b-non-reasoning)       | 10.81 | yes              |       71.21% | 34.67% |
| [Qwen3.5 4B (Reasoning)](https://artificialanalysis.ai/models/qwen3-5-4b)                         | 13.12 | yes              |       77.07% | 63.00% |
| [Qwen3 Coder 30B A3B Instruct](https://artificialanalysis.ai/models/qwen3-coder-30b-a3b-instruct) |  9.59 | yes              |       51.62% | 32.67% |

Coverage gaps: **TinyLlama 1.1B Chat** and **Qwen2.5-Coder 1.5B Instruct** (NPU
experiments) have no matching row in this retrieved leaderboard; no substitute
model score is assigned. Qwen3.5 4B is included as a tested long-context candidate,
and Qwen3-Coder 30B-A3B as the Colibri candidate. All six distinct installed local
models are represented; seven picker profiles do not imply seven base models.

The new CSV retains September's columns and adds the estimated flag, GPQA Diamond,
`terminalbench_hard` (raw AA field, **not** Terminal-Bench 4.0), source URL, retrieval
date, and index version. Evaluation fractions use 0–1, except the 0–100 index and
AA-Omniscience. Numeric zero remains zero. Use the same Flight decoding recipe
below; select September's 57 slugs plus the 13 linked rows above, without filtering
estimated indices. Only this selected cohort is redistributed.

For actual runtime, retrieval, and quantization observations, see
[the rugged inference report](../../cluster/docs/inference/rugged_local_llm.md).
Do not infer local serving cost or latency from AA's API price columns.

## Historical snapshot: 2026-09-24

<cited_models_2026_09_24.csv> refreshes the model/effort slugs cited in
<../ai_subscription_comparison.md> and adds all published GPT-6 Luna, Sol, and Astra
efforts plus `qwen3-8-flash-next` (added same-day from the same leaderboard fetch for
an agent-routing question; not cited in the subscription analysis). It contains
**57 measured rows: 39 from the historical cohort, 17 GPT-6 rows, and
Qwen3.8-Flash-Next**. Fourteen historical slugs no longer have a measured current
index and are excluded, not filled with their August scores:

```text
claude-sonnet-5-non-reasoning
glm-4-6-reasoning
glm-4-7
glm-5-2-non-reasoning
gpt-5
gpt-5-4
gpt-5-6-sol-non-reasoning
gemini-3-7-flash-low
gemini-3-7-flash-medium
gemma-4-31b-non-reasoning
gemma-4-31b
kimi-k3-low
mimo-v2-5-0424
gpt-oss-120b-low
```

- Fetched from <https://artificialanalysis.ai/leaderboards/models> on 2026-09-24.
- Index methodology: **v4.3.2**, as published at
  <https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index>.
- Scores and costs are copied from the same leaderboard response without rounding.
  Blank means unavailable, not zero. Estimated index rows are excluded.
- Costs are API list-price costs per benchmark-weighted index task, **not** costs
  per successful task, costs per Terminal-Bench task, or subscription usage charges.

Do not compare index scores or task costs across these dated snapshots as a model
improvement/regression: the evaluation mix changed. In particular, September uses
Terminal-Bench 4.0, not August's 2.1.

### GPT-6 cost/effort comparison

Selected rows from the CSV; Terminal-Bench is a separate score, not the workload
whose cost appears in the adjacent column. These are reference points for agent
routing, not estimates of success on our repository tasks.

| Model / effort | Index | Index cost/task | Terminal-Bench 4.0 |
| -------------- | ----: | --------------: | -----------------: |
| Luna high      | 32.15 |        $0.02862 |              4.55% |
| Luna xhigh     | 33.88 |        $0.04171 |              8.08% |
| Luna max       | 37.26 |        $0.06809 |             12.63% |
| Sol low        | 33.90 |        $0.13224 |              9.09% |
| Sol medium     | 39.78 |        $0.24820 |             18.69% |
| Sol high       | 42.82 |        $0.37463 |             26.26% |
| Astra low      | 45.78 |        $0.81751 |             41.92% |
| Astra high     | 50.92 |        $1.72525 |             54.04% |

Luna max costs **51.5% as much as Sol low**, with higher scores on both listed
metrics in this snapshot. Sol high costs **13.09x Luna high**; Astra high costs
**60.28x Luna high**. Those are observed benchmark-task cost ratios, not the
20x/100x ratios of their input/output token list prices. Different effort settings
and token consumption matter.

### Export schema and refresh

The September public leaderboard no longer exposes the August export's four cost
legs or token totals. This snapshot omits `suite_cost_usd`, `cost_*_usd`, derived
token/cache accounting, and the coding/agentic sub-indices rather than reusing stale
values. Only seven raw evaluation fields are available here; this is not a full
export of the ten-evaluation v4.3.2 suite.

The AA key is in
<../../cluster/k8s/external-creds/analysis-ai.sops.yaml>, under `stringData.api-key`.
On this refresh it authenticated as **Free**: `/api/v2/language/models` returned
403 (Pro required), while `/api/v2/language/models/free` returned four pages of 200,
673 rows total, with `intelligence_index_version: 4.3`. The API omits patch versions.
Its rounded index and cost/task values were used to cross-check the selected rows;
the CSV retains the public page's precision. See <https://artificialanalysis.ai/data-api/docs>.
Never log the decrypted key or place it in command-line arguments.

To reproduce the selection, fetch the leaderboard HTML and JSON-decode the string
argument of each `self.__next_f.push([1, ...])`. Concatenate those strings, parse
the JSON payload after each Flight record's colon, and recursively decode nested
JSON strings. Model rows have `slug` and `intelligenceIndex`; exclude null indices
and `intelligenceIndexIsEstimated: true`. Select the 53 August CSV slugs plus
`gpt-6-*` plus `qwen3-8-flash-next`, then sort by slug. Do not decode strings with
`unicode_escape` or split CSV rows naively on commas: names can contain commas.

| CSV field                                             | Leaderboard field                                |
| ----------------------------------------------------- | ------------------------------------------------ |
| `model`, `slug`, `creator`                            | `name`, `slug`, `modelCreatorName`               |
| `intelligence_index`                                  | `intelligenceIndex`                              |
| `cost_per_task_usd`                                   | `intelligenceIndexCostPerTask`                   |
| `price_1m_input_usd`, `price_1m_output_usd`           | `price1mInputTokens`, `price1mOutputTokens`      |
| `price_1m_cache_read_usd`, `price_1m_cache_write_usd` | `cacheHitPrice`, `cacheWritePrice`               |
| `context_window_tokens`                               | `contextWindowTokens`                            |
| `gdpval_aa`, `terminalbench_v4_0`, `scicode`          | `gdpvalNormalized`, `terminalBench40`, `scicode` |
| `hle`, `critpt`, `omniscience`, `aa_lcr`              | `hle`, `critpt`, `omniscience`, `lcr`            |

Map null and Flight's `$undefined` sentinel to blank, preserving numeric zero.
Evaluation scores use 0–1 except the index (0–100) and AA-Omniscience (which can be
negative). Reasoning effort is part of AA's model name and slug, not a Boolean.
Record the methodology version again on every refresh; a date alone is insufficient.

## Historical snapshot: 2026-08-23

<cited_models_2026_08_23.csv> remains unchanged because the dated subscription
analysis calculates its tables from those figures. Its distinct schema, population,
and cost-accounting caveats are documented in <snapshot_2026_08_23.md>.
