# GitHub recent-run repair sizing

Measured on 2026-11-06 at 04:41 UTC, while preparing PR #14553, from the first six pages of `GET /repos/agentydragon/ducktape/actions/runs?per_page=100`. These are workflow run records, including skipped helper workflows, rather than workflow definitions or PR counts.

| Observation                          | Result                                       |
| ------------------------------------ | -------------------------------------------- |
| Sample                               | 600 runs, 03:31:45–04:40:20 UTC              |
| Latest 100                           | 04:28:49–04:40:20 UTC: 11 minutes 31 seconds |
| Most recent hour in sample           | 467 runs                                     |
| Most recent 15 minutes in sample     | 154 runs                                     |
| Largest rolling 15 minutes in sample | 218 runs                                     |
| Latest 100 from `Publish PR visuals` | 67                                           |
| Unfinished runs outside latest 100   | 95                                           |
| Distinct workflow IDs                | 17                                           |

For example, [run 42163877682](https://github.com/agentydragon/ducktape/actions/runs/42163877682) was the newest sampled run. The old one-page poll every 15 minutes could miss runs even without an outage. Creation-ordered pagination also means old unfinished runs can leave the repair window before they finish.

The revised poll keeps the 15-minute repair cadence and follows pagination until a page crosses a one-hour creation cutoff. Twelve requests permit up to 1,200 records per poll, compared with 872/hour if the observed peak quarter-hour repeated four times. This is headroom against a short sample, not a long-term capacity bound. Twelve pages per poll also cap normal page traffic at 48/hour, leaving 12 of the public IP's 60 unauthenticated requests/hour for other callers and retries. That spare capacity is not reserved; shared traffic or retries can still exhaust the quota. Guards and rate limiting can leave a poll incomplete.

Webhooks remain the live collection path. This poll is bounded recent-run repair. Reliable recovery of older queued runs, long outages, and missed job events needs authenticated backfill and explicit reconciliation of unfinished run IDs; increasing a page count alone does not provide that guarantee.
