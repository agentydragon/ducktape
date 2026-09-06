# GraphQL quota acceptance monitoring

Investigation: [#5213](https://github.com/agentydragon/ducktape/issues/5213).
The temporary Desktop route block has causal suppression/reversal evidence;
that is not a completed multi-day reliability result. Keep the issue open.

## Acceptance boundary

Wyrm2's standing block began September 6 at 01:35:27 UTC. The first clean
post-mitigation reset was observed at **2026-09-06T01:35:46Z**, Unix
`1788658546`. Check after 48 hours; prefer seven days, ending
**2026-09-13T01:35:46Z**, Unix `1789263346`, before declaring resolved.

Require normal representative Desktop use, no observed exhaustion, independent
account-counter coverage, and verified mitigation/capture coverage. A quiet
account with a closed app is not the same experiment. Source opt-in is not a
rollout: rugged is configured in #5675 but has not been live-activated.

Uncovered intervals cannot count as healthy. Recover stronger independent
evidence or extend the covered observation window. A recurrence with the block
verified active requires attribution of the residual consumer, not declaring
the mitigation successful because average usage fell.

## Deployed measurement contract

The GraphQL exporter performs a fresh rate-only request on each `/metrics`
scrape, validates the resource and four quota headers, and returns gauges only
on success. Fetch/timeout/header failures return 502. Therefore a successful
scrape establishes a successful upstream fetch; `/healthz` alone does not.
REST `/rate_limit.resources.graphql` is not a substitute for this counter.

Live scraping is once per minute, with a 15-second scrape timeout and
10-second upstream timeout. The personal job is
`github-graphql-rate-exporter-agentydragon`; its `up` series has no
`github_account` label. Mimir retains blocks for 365 days with no live runtime
override. Seven-day-old data was queried successfully; the quota exporter
itself has only roughly two days of history at investigation time.

The block's private local event log records thirty-second heartbeats and
cumulative counts per process lifetime. Check heartbeat continuity, enabled
state, raw-capture availability, and actual client routing separately from
account usage. Subtract known synthetic probes from natural-request counts.
No central block-health collector or automatic issue-closing job is deployed.

## Reproducible queries

These examples use the verified snapshot **2026-09-06T01:59:05Z**, Unix
`1788659945`, 1399 seconds after the start. Fix the API query's `time` to that
timestamp. For later checks replace `1399s` with the elapsed duration; at the
seven-day endpoint use `7d` and `time=1789263346`.

Minimum remaining:

```promql
min(min_over_time(github_graphql_rate_remaining{github_account="agentydragon"}[1399s]))
```

Result: **4999**. There were **24** retained post-start quota samples:

```promql
sum(count_over_time(github_graphql_rate_remaining{github_account="agentydragon"}[1399s]))
```

Last retained zero since start; an empty result means none observed:

```promql
max(max_over_time(((timestamp(github_graphql_rate_remaining{github_account="agentydragon"}) and (github_graphql_rate_remaining{github_account="agentydragon"} == 0)) >= 1788658546)[1d:15s]))
```

Result: **empty**. At the seven-day endpoint change `[1d:15s]` to `[7d:15s]`.
Apply `timestamp` before filtering: `timestamp(metric == 0)` reports subquery
evaluation times rather than the original sample timestamps. The last raw zero
before the start was **01:34:58.191Z**, Unix `1788658498.191`.

One-minute-grid fresh quota coverage, allowing a 90-second sample age:

```promql
avg_over_time(((max((time()-timestamp(github_graphql_rate_remaining{github_account="agentydragon"})) < bool 90) or vector(0)) and on() (vector(time()) >= 1788658546))[1399s:1m])
```

Result: **1.0**. Missing series become zero rather than disappearing from the
denominator. This is sampled freshness, not proof of every scheduled scrape.
Check successful upstream-fetch coverage on the same grid:

```promql
avg_over_time(((max((up{job="github-graphql-rate-exporter-agentydragon",namespace="monitoring"} == 1) and ((time()-timestamp(up{job="github-graphql-rate-exporter-agentydragon",namespace="monitoring"})) < 90)) or vector(0)) and on() (vector(time()) >= 1788658546))[1399s:1m])
```

The `max` unions old and replacement targets. Simple summed success/attempt
ratios can falsely penalize a healthy replacement when an old failed target
remains discovered. Earlier fixed-time verification at 01:42:03Z showed seven
quota samples, seven successful scrapes, and both grid-coverage measures 1.0.

The previous 24 hours were **not fully covered**: fresh-grid coverage was
97.2917%, with eight raw-sample gaps longer than 90 seconds. The longest was
28m02.738s, September 5 at 08:13:55.454–08:41:58.192Z.

## Alerts are not delivery proof

The deployed exhaustion rule waits for fifteen continuous minutes at zero.
An exhaustion near hourly reset can be recorded without ever alerting.
[PR #5680](https://github.com/agentydragon/ducktape/pull/5680) proposes a
five-minute sampled-zero latch and missing-observation detection, preserving
generic `TargetDown` ownership of explicitly failed scrapes. It was not applied
live during the measurements above.

GitHub alerts route to the default `ntfy` receiver. Its secret-file URL was not
read. Group wait is thirty seconds; group interval five minutes; repeat two
hours. Healthy rule evaluation alone does not prove receipt.

At 01:59:05Z, one-hour webhook metrics for `monitoring-alertmanager` showed
approximately 63 attempts and zero failures on `alertmanager-monitoring-0`,
versus 62 attempts and 52 failures on `alertmanager-monitoring-1`. Replica 1's
01:57:31 logs showed TCP connection timeouts to the ntfy destination. Replica
0's earlier 23:59 failures were HTTP 429, provider code 42908, daily message
quota reached. These are distinct observed failure modes, not proof of one
common cause or failure of any particular GitHub notification.

Metrics lack receiver/alertname labels; logs identify the shared `ntfy` path.
No synthetic notification was sent. End-to-end delivery remains unverified.
[Subsequent transport checks](notification_delivery.md) reproduce the failing
replica's source-specific TCP path outside the Alertmanager process as well.

## Interpretation at the end of the window

Save fixed-time query results and raw-sample gap analysis, reconcile block
heartbeats/process lifetimes and client use, and inspect any captured rate-limit
errors. Report exhaustion, collection gaps, and delivery gaps separately.

One-minute sampling cannot observe an exhaustion and reset entirely between
samples. Retain direct response/error evidence when available. The final claim
must state its coverage and this visibility limit; neither passing tests nor
an empty zero query makes the underlying reliability goal automatically complete.
