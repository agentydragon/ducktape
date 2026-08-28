# Artificial Analysis Intelligence Index — cited rows

`cited_models_2026_08_23.csv` holds the rows <../ai_subscription_comparison.md> quotes,
so its per-model numbers can be checked rather than taken on trust.

**Only the cited rows are here, not the corpus.** Artificial Analysis sells
redistribution rights and states that free access is "for exploration and internal
workflows only"; a public mirror of all 169 rows is close to the product. Quoting the
rows actually used keeps that document checkable without republishing the dataset. The
population-level findings below therefore state their result and the recipe that
reproduces it, rather than shipping the data behind them.

## Provenance

- **Source:** <https://artificialanalysis.ai/leaderboards/models>
- **Fetched:** 2026-08-23
- **Attribution:** all index scores, prices and eval results here are Artificial
  Analysis's, and their use requires crediting <https://artificialanalysis.ai/>.
- **Index:** Artificial Analysis Intelligence Index, nine evals — `gdpval-aa`,
  `tau3-banking`, `terminalbench-v2-1`, `scicode`, `humanitys-last-exam`,
  `gpqa-diamond`, `critpt`, `omniscience`,
  `artificial-analysis-long-context-reasoning`. Methodology:
  <https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index>

**Why this is scraped rather than pulled from the data API.** AA publishes one
(<https://artificialanalysis.ai/data-api/docs>). Checked against the live free tier on
2026-08-28 — `GET /api/v2/language/models/free`, four pages of 200, 624 rows — it
supplies more than expected and still cannot produce this table:

| Needed here                              | Free tier                                              |
| ---------------------------------------- | ------------------------------------------------------ |
| One row per model per reasoning effort   | **yes** — `claude-opus-5-xhigh`, `gpt-5-6-luna-low`, … |
| Intelligence, coding and agentic indices | **yes**, but rounded to one decimal                    |
| All four list prices                     | **yes**, rounded — a $0.0028 cache price reports `0.0` |
| Cost per task split into four cache legs | **no** — only `cost_per_task.total_cost`, a scalar     |
| Per-task token counts                    | **no**                                                 |
| The nine raw eval scores                 | **no** — `evaluations` carries only the three indices  |

The scalar cost is the disqualifying one: `tokens_per_task`,
`cache_read_share_of_input`, `effective_usd_per_m_tokens` and
`cache_accounting_coherent` all come from inverting the four legs separately, and a
total cannot be inverted. The 1-decimal rounding would also coarsen every index in this
file, and the raw eval columns would be lost outright.

The free tier is additionally "for exploration and internal workflows only", with
redistribution reserved to a commercial licence — a live consideration for this file,
which is why only the cited rows are committed.

The records ship inside the leaderboard page's Next.js flight payload. To refresh,
fetch the HTML and pull the model objects out of it:

```python
import json, re
chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
blob = "".join(chunks).encode().decode("unicode_escape", errors="replace")
# then brace-match forward from each `{"id":"<uuid>","name":"` to get one JSON object per model
```

## Population

**53 rows, 24 models** — every model <../ai_subscription_comparison.md> names, at every
reasoning-effort setting AA scored it at. One row per model **per effort setting**:
`Claude Opus 5 (max)` and `Claude Opus 5 (medium)` are separate rows of the same model,
and the spread between them is large enough that collapsing them loses the point.

The snapshot they came from held **169 rows — every model with a measured index**,
matching the leaderboard's own count ("out of 169 models ranked"). AA also carries ~434
models with an _estimated_ index; those were excluded, and none has a cost figure
anyway. Any claim in the document quantified over "169 models" refers to that snapshot,
not to this file; re-run the recipe above to check one.

Within this file, `cost_per_task_usd` is empty for 1 row AA scored but did not price,
and 5 rows carry `cache_accounting_coherent=no`.

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
| `cache_accounting_coherent`     | `no` where AA books cache writes but zero cache reads — see below                                                         |
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
is meaningless without the cache behaviour beside it.

## Where the cache accounting is incoherent

**39 of the 169 models in the source snapshot book a nonzero `cost.cacheWrite` against
exactly zero `cost.cacheRead`** — 5 of them are in this file. A loop that writes a cache and never reads it does not describe
anything real, so for those models AA has evidently attributed the replayed context to
writes. In six of them the contradiction is explicit in AA's own fields: a
`cacheHitDiscountPercent` of 80% sits beside a `cacheHitPrice` equal to the
undiscounted input price. Cache writes are 88.5-99.6% of input cost across the affected
set (median 98.7%), so the token split is dominated by whichever fallback price is
assumed.

Affected rows carry `cache_accounting_coherent=no` and leave `tokens_per_task`,
`cache_read_share_of_input` and `effective_usd_per_m_tokens` **blank**. Their
`cost_per_task_usd` and index scores are unaffected and remain usable.

Do not read a 0.0% cache share as "this model has no prompt caching". `GLM-4.7
(Reasoning)` is flagged here, and <../zai_api.md> records a direct measurement that it
caches: `cached_tokens` goes `0` to `12544` on a follow-up call sharing a ~12.5k-token
prefix.

Everything is **API list price**. A subscription's economics do not follow from it —
that conversion is what the comparison document exists to do.
