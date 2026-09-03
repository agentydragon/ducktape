# GitHub rate-limit monitoring is blind to the limit that actually bites

Status: open. Investigated 2026-09-03 against ~89h of Mimir data (retention starts
2026-08-31 04:17 UTC — the exporters are only ~4 days old).

## Finding

`github-exporter` cannot observe the exhaustion. It scrapes REST `/rate_limit`, which
reports the GraphQL bucket as permanently full. Measured at the same instant, while
every GraphQL call was returning 403:

```text
GraphQL `rateLimit` field:  limit=5000 remaining=0    used=10783  resetAt=21:56:58Z
REST /rate_limit .graphql:  limit=5000 remaining=5000 used=0
```

GraphQL was 2.16x over budget; `/rate_limit` reported it untouched. `search` behaves the
same way — 403 `API rate limit exceeded for user ID 714892` while `/rate_limit` showed
30/30. Only `core` is reported truthfully.

This makes the current dashboard's "GraphQL quota remaining" panel actively misleading:
`github_rate_remaining{resource="graphql"}` sat flat at 5000 for the entire 65h life of
the current pod.

## What the captured data does say

`core` is not the problem and never has been. Over 89h it never dropped below
4261/5000; the worst hour consumed 723 (14.5% of budget), the median hour ~100.
Buckets are aggregated per user — the exporter's token and the local `gh` token report
identical `used`/`reset` — so that 4261 floor is the true aggregate across every
agentydragon token. CI and cluster processes are exonerated on `core`.

Two `core` patterns, both benign:

- An 8-hourly job at ~01:58 / 09:58 / 17:58 UTC costing 150-180 requests. Gaps between
  occurrences: 7.98, 8.02, 8.00, 8.00, 8.05, 7.95, 8.00h. Not a k8s CronJob, not a
  GitHub Actions cron (`sync-pins` is `*/30` and mints a GitHub App token, so it bills
  the App installation, not user 714892). Unidentified; at 3.4% of budget, not urgent.
- A ~32/h floor on idle hours, consistent with a ~2-minute poller.
- One outlier: 2487 requests in the 08-31 05:00 UTC hour, concurrent with the only
  GraphQL disturbance in the window. That is this exporter's own merge (#5328, 05:14 UTC)
  and the CI run that first published its image (05:19 UTC) -- development traffic, not a
  recurring pattern.

`github_rate_remaining{resource="graphql"}=0` samples on 08-31 flapping 5000->0->5000
at 1-minute spacing are exporter artifacts, not consumption — a reset cannot fire four
times in 20 minutes.

The `agentydragon-agent` account is effectively unused: 20 core requests in 89h. Whatever
is burning GraphQL authenticates as `agentydragon`.

## The instrument that works

`cluster/exporters/github_graphql_rate_limit` reads the `x-ratelimit-*` headers off a
GraphQL response, which carry the real GraphQL counters. It was built and published by CI
(`devinfra/ci/image_targets.json`) since 2026-08-31 but nothing under `cluster/k8s/`
deployed it until this change.

Its probe is `query { rateLimit { cost } }`. A rateLimit-only query costs 0 points and
GitHub still serves it during exhaustion — verified: that query returned 200 with
`remaining=0` in the same second that `{ viewer { login } }` returned 403. So the
exporter keeps reporting exactly when the account is blocked, rather than going blind,
and scraping it every minute does not consume the quota it measures.

## Next steps

Deployed by this change: the exporter for both accounts, and the dashboard's GraphQL
panels repointed at `github_graphql_rate_*`. `github_rate_*{resource="graphql"}` is
dropped from the by-resource panel rather than left beside the resources REST reports
correctly -- that series is known-false, not merely absent.

Still open:

- Alert on `github_graphql_rate_remaining` approaching 0. This is the condition the open
  issue is actually about, and nothing pages on it yet.
- `/metrics` raises 502 on probe failure, so an exhausted account and a genuine outage
  are indistinguishable from the scrape's perspective. `up{job=...}` going 0 is the only
  current signal, and it does not say which.
- `search` (30/min) and `code_search` (10/min) are per-minute buckets; a 1-minute
  ServiceMonitor interval cannot resolve them, and REST `/rate_limit` misreports `search`
  the same way it misreports `graphql`. They remain unobservable here.
- Attribution. Now that GraphQL is measurable, ask which consumer is responsible.
  Candidates all authenticate as `agentydragon` and are GraphQL-heavy: `gh pr`/`gh issue`
  from agents, and the GitHub MCP server's PR/check polling.
- The 8-hourly `core` job at ~01:58 / 09:58 / 17:58 UTC is still unidentified. At 3.4% of
  budget it is not urgent.
