# Artificial Analysis — selected model snapshots

Source and attribution: [Artificial Analysis](https://artificialanalysis.ai/).
These are selected comparison rows, not a mirror of AA's full dataset. Retain that
scope when refreshing; API access does not itself grant redistribution rights.

## Current snapshot: 2026-09-24

<cited_models_2026_09_24.csv> refreshes the model/effort slugs cited in
<../ai_subscription_comparison.md> and adds all published GPT-6 Luna, Sol, and Astra
efforts. It contains **56 measured rows: 39 from the historical cohort and 17 GPT-6
rows**. Fourteen historical slugs no longer have a measured current index and are
excluded, not filled with their August scores:

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
`gpt-6-*`, then sort by slug. Do not decode strings with `unicode_escape` or split
CSV rows naively on commas: names can contain commas.

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
