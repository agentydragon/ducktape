# Weekly subscription token-capacity estimates

**Analysis run:** 2026-10-31 17:15:04 PDT (2026-11-01 00:15:04 UTC).
**Evidence bundle generated:** 2026-10-31 11:10 UTC. This uses the latest saved local evidence I found; the source systems were not re-queried for this run.

These are estimates of recorded total tokens per full 100 percentage points of a provider's weekly quota gauge, assuming one model uses the whole weekly budget. They are not vendor-published token limits.

## General weekly quota gauges

The reference point and bootstrap interval use the estimator's `sum_sources`, zero-lag scenario. The final column is the outer envelope of publishable 90% intervals across six scenarios: `sum_sources` and `max_source_family`, each at 0-, 5-, and 15-minute metering lag. Values are billions of tokens. The envelope captures scenario sensitivity; it is not a single probability interval. The general-gauge rows passed all six scenarios; the Fable-scoped row passed four because its 15-minute-lag fits failed support checks.

| Weekly meter       | Model ID            | Reference point | Conditional 90% bootstrap interval | Six-scenario interval envelope |
| ------------------ | ------------------- | --------------: | ---------------------------------: | -----------------------------: |
| Anthropic / Claude | `claude-fable-5-1`  |            2.0B |                           1.6–2.3B |                       1.6–2.9B |
| Anthropic / Claude | `claude-opus-5-5`   |            8.3B |                          6.3–10.1B |                      5.9–10.4B |
| Anthropic / Claude | `claude-sonnet-5-5` |           11.9B |                          5.5–15.5B |                      5.2–16.4B |
| OpenAI / Codex     | `gpt-6-astra`       |            1.0B |                           0.9–1.2B |                       0.8–1.2B |
| OpenAI / Codex     | `gpt-6-luna`        |           31.6B |                         15.4–57.8B |                     13.2–98.7B |
| OpenAI / Codex     | `gpt-6-sol`         |            4.6B |                           4.1–5.9B |                       4.1–7.0B |
| OpenAI / Codex     | `gpt-6.1-sol`       |            6.8B |                           6.0–9.2B |                       4.9–9.2B |

Claude general weekly calibration retained 59 distinct days from accepted quota polls between 2026-08-28 and 2026-10-31. OpenAI retained 24 days from accepted polls between 2026-10-07 and 2026-10-31. Gaps and rejected intervals mean these spans are not continuous observation.

### Claude Fable scoped weekly gauge

Claude also exposes a separate `Fable` weekly percentage gauge. It has its own scale; do not add it to or equate it with the general Claude gauge.

| Meter                          | Model ID           | Reference point | Conditional 90% bootstrap interval | Six-scenario interval envelope |
| ------------------------------ | ------------------ | --------------: | ---------------------------------: | -----------------------------: |
| Anthropic / Claude Fable scope | `claude-fable-5-1` |            1.0B |                           0.7–1.1B |                       0.7–1.1B |

The Fable-scope fit retained 53 distinct days from accepted scoped polls between 2026-08-28 and 2026-10-30. The latest four-week fit did not have enough recent Fable usage to publish an estimate. The meaning of the scope's `is_active` flag and its relationship to the general Claude gauge are not established, so the general and scoped Fable estimates are separate observations, not additive allowances.

## Recent-window check

For the latest approximately 28 days ending 2026-10-31, the OpenAI fit still supports the four models in the table, with the same point estimates and conditional intervals. `gpt-6.1-terra` had only three active days and did not pass the estimator's support checks.

For Claude, only `claude-sonnet-5-5` passed recent-window support checks: **10.3B** per full weekly gauge, with a conditional 90% interval of **3.5–11.7B**. `claude-opus-5-5` had seven active days and `claude-fable-5-1` had two, so this window does not support current-only estimates for them. The full-period Opus and Fable rows above are conditional historical fits, not current entitlements.

The estimator did not publish a value for `codex-auto-review` because its quota cost was indistinguishable from zero within the bootstrap interval. It also withheld Claude estimates for `claude-fable-5`, `claude-haiku-4-5-20251001`, `claude-opus-4-8`, `claude-opus-5`, and `claude-sonnet-5` because their fit support or validation was insufficient. No capacity estimate is available for other providers from these captures; the calibrated weekly gauges here are Claude and Codex.

## Inputs and method

The run used the six local aggregate inputs described in [usage_quota.md](usage_quota.md): quota polls, raw accounting, native five-minute usage, Claude Code Web five-minute usage, Langfuse five-minute usage, and Claude scoped quota. Private inputs and full generated estimator output remain in ignored local paths.

The estimator at code revision `cd2f19e3` fit nonnegative quota-per-token coefficients to exact model usage over poll-aligned, monotonic ranges, using 60-minute target chunks. A 100-point gauge spend is the model's point estimate; the conditional interval uses 500 resamples of contiguous three-calendar-day blocks and the 5th–95th percentiles. It is conditional on the captured source accounting, payment scope, model, source-overlap case, and lag. It does not include missing or unobserved usage, uncertainty in subscription attribution, integer gauge quantization, or policy changes.

Evidence watermarks (UTC):

- Weekly quota polls: 2026-10-31 08:44:01.
- Native Rugged snapshot: 2026-10-31 08:45:04; Wyrm2 scan: 09:08:48.
- Claude Fable scoped quota: 2026-10-31 09:04:10.
- Langfuse last populated bucket: 2026-10-31 01:50.
- Claude Code Web: 2026-10-28 02:39:19; discovery and sync coverage remain uncertain.

Unknown overlap between sources can make `sum_sources` count the same request more than once. Conversely, `max_source_family` does not prove a strict lower bound if sources contain distinct requests. Unknown native and Web payment lanes were included under the stated assumption that most such traffic uses subscriptions; unconfirmed Langfuse subscription traffic was excluded. These assumptions and incomplete capture are outside the bootstrap intervals.

The subscription's weekly quota is shared across its models. The rows are counterfactual one-model-at-a-time capacities and cannot be added. Model token mix, caching, reasoning effort, resets between polls, and current plan policy can all change realized capacity. A reset wholly between polls can remain undetected when usage masks the quota drop.
