# Artificial Analysis Intelligence Index — committed snapshot

`intelligence_index_2026_08_23.csv` is the per-model data behind
<../ai_subscription_comparison.md>, so its numbers can be re-derived rather than
taken on trust.

## Provenance

- **Source:** <https://artificialanalysis.ai/leaderboards/models>
- **Fetched:** 2026-08-23
- **Index:** Artificial Analysis Intelligence Index, nine evals — `gdpval-aa`,
  `tau3-banking`, `terminalbench-v2-1`, `scicode`, `humanitys-last-exam`,
  `gpqa-diamond`, `critpt`, `omniscience`,
  `artificial-analysis-long-context-reasoning`. Methodology:
  <https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index>

The leaderboard has no data API; the records ship inside the page's Next.js flight
payload. To refresh, fetch the HTML and pull the model objects out of it:

```python
import json, re
chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
blob = "".join(chunks).encode().decode("unicode_escape", errors="replace")
# then brace-match forward from each `{"id":"<uuid>","name":"` to get one JSON object per model
```

## Population

**169 rows — every model with a measured index**, which is exactly the leaderboard's
own stated count ("out of 169 models ranked"). AA also carries ~434 models with an
_estimated_ index; those are excluded, and none of them has a cost figure anyway.

One row per model **per reasoning-effort setting** — `Claude Opus 5 (max)` and
`Claude Opus 5 (medium)` are separate rows of the same model, and the spread between
them is large.

`cost_per_task_usd` is empty for 17 rows AA has scored but not priced.

## Columns

| Column                          | Meaning                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `intelligence_index`            | The nine-eval aggregate, 0-100                                                                                            |
| `coding_index`, `agentic_index` | AA's coding and agentic sub-aggregates                                                                                    |
| `cost_per_task_usd`             | API cost of one index task, weighted across the nine evals — the chart's x-axis                                           |
| `suite_cost_usd`                | API cost of running the whole index suite once                                                                            |
| `price_1m_*`                    | List price per 1M tokens, including cache read and cache write                                                            |
| `cost_*_usd`                    | AA's per-task cost split into non-cached input, cache reads, cache writes, output — these four sum to `cost_per_task_usd` |
| `tokens_per_task`               | **Derived**, not published: each `cost_*_usd` leg divided by its matching price, summed                                   |
| `cache_read_share_of_input`     | Cache reads as a fraction of input tokens per task (0-1)                                                                  |
| `effective_usd_per_m_tokens`    | **Derived**: `cost_per_task_usd` / `tokens_per_task` — the real blended rate, cache included                              |
| `gdpval_aa` … `aa_lcr`          | The nine raw eval scores, 0-1 (`omniscience` is 0-100)                                                                    |

## The derived columns

AA publishes cost per task but not tokens per task. The last three columns invert the
cost model to recover it: `cost.nonCacheInput / price1mInputTokens`,
`cost.cacheRead / cacheHitPrice`, `cost.cacheWrite / cacheWritePrice` and
`cost.output / price1mOutputTokens`, summed. Where a cache price is absent the input
price is used.

**The derivation is validated by an independent field.** Its output leg reproduces
AA's separately published `intelligenceIndexOutputTokensPerTask` exactly, to the
token, for every model checked — so the inversion is recovering AA's real accounting
rather than approximating it.

Do **not** instead divide `intelligenceIndexTokenCounts` totals by an inferred task
count. Those totals and the per-task figures use different weightings across the nine
evals, and doing so understates tokens per task by 2.5-6.7x depending on the model.

Cache reads run 86-96% of input tokens for most models, so any figure quoted in tokens
is meaningless without the cache behaviour beside it. `GLM-4.7 (Reasoning)` is the
instructive exception at 0.0%.

Everything is **API list price**. A subscription's economics do not follow from it —
that conversion is what the comparison document exists to do.
