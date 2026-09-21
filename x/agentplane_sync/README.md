# Agentplane Electric sync derisk spike

This folder is an isolated experiment for the conversation-view sync design in
[PR #7479](https://github.com/agentydragon/ducktape/pull/7479). It does not
change the Agentplane runner, application, database, or deployed behavior. The
projector consumes the existing durable `EventEntry` protocol and builds a small
PostgreSQL read model; a Python proxy authorizes Electric shape requests; a
React browser uses TanStack DB. Nothing here is connected to an application
runtime yet.

## Run the experiment

Run the one test through Bazel and BuildBuddy. Its PostgreSQL, Electric, and
Chromium containers require RBE:

```bash
nix develop --command bbr test //x/agentplane_sync:e2e_test --test_output=errors
```

The test writes its HAR, screenshots, Playwright trace, SQL plans, request
measurements, and container logs to Bazel undeclared test outputs. After a run,
retrieve them from the BuildBuddy invocation:

```bash
nix develop --command bbapi artifact list <invocation-id> --kind test
nix develop --command bbapi artifact download <invocation-id> electric-derisk/browser.har
```

The test creates histories with 1,000 and 50,000 rows, uses cursors above
JavaScript's exact integer range, and checks a 30-row tail plus exclusive
older-page reads. `query-plans.json` includes `EXPLAIN ANALYZE` results for the
tail and older-page SQL. Tests deliberately fail when asserted behavior does
not occur; a passing frontend build is not a passing integration experiment.

## Pinned components

| Component | Pin | Source |
| --- | --- | --- |
| PostgreSQL | repository `postgres_18` OCI digest | `third_party/containers` |
| Electric standalone service | 1.8.0, Linux amd64 OCI digest in `MODULE.bazel` | [Electric HTTP API](https://electric.ax/docs/sync/api/http) |
| Electric TypeScript client | `@electric-sql/client` 1.5.28 | `package.json` / `pnpm-lock.yaml` |
| TanStack Electric collection | `@tanstack/electric-db-collection` 0.4.7 | `package.json` / `pnpm-lock.yaml` |
| TanStack React DB | `@tanstack/react-db` 0.3.7 | `package.json` / `pnpm-lock.yaml` |

The old workspace pin `@tanstack/db` 0.9.2 is not used by this spike. The
selected React DB and Electric collection resolve the same `@tanstack/db` 0.8.7
core in the lockfile; importing them alongside the workspace's older core would
otherwise create incompatible collection types.

The browser requests `log=changes_only`, starts its shared stream at `offset=now`,
and uses Electric subset snapshots for the tail and history pages. The adapter
emits legacy `subset__*` GET parameters in this pinned version; the proxy
validates and forwards them with a 30-row maximum. A query-plan check separately
guards against bounded response sizes hiding an unbounded PostgreSQL scan.

## Evidence matrix

The full RBE run [b48b37b5](https://app.buildbuddy.io/invocation/b48b37b5-c9d3-425c-9ace-c5a2bf569d51)
passed `//x/agentplane_sync:e2e_test`. Its HAR, browser request journal, SQL
plans, screenshots, traces, service logs, and summarized measurements are
available as undeclared test outputs. This is prototype evidence from one test
environment, not a production load test.

| Gate | Status | Evidence or open question |
| --- | --- | --- |
| Projection idempotency, two workers racing one prefix, failed-batch rollback, retry | Confirmed | `e2e-summary.json` records one writer, duplicate events for the losing worker, unchanged checkpoint/payload after rollback, and a successful retry. |
| Bounded SQL work | Confirmed | `query-plans.json` covers the 50,000-row tail and exclusive-before queries. The test requires `sync_view_row_tail` and at most 30 actual rows from each bounded index scan. |
| Exact bigint cursors and exclusive-before history | Confirmed | Python API and browser preserve `9007199254740993`; the loaded page has anchors strictly before the tail cursor. During the held page response, newer text and completion events commit, and the browser renders the newer revision with `complete` status. |
| Initial bootstrap for 1,000 and 50,000 rows | Confirmed | Both real Electric snapshots load 30 rows. Captured response sizes were 19,092 bytes and 19,100 bytes respectively; the large history did not inflate the initial transfer. Shape rows omit text, arguments, output, reasoning, and content fields. |
| Scroll anchor pixel preservation | Confirmed | `scroll-evidence.json`: anchor top 175.875px before loading, 1,135.875px before correction, correction 960px, and final displacement 0px. |
| Any-item and out-of-order updates; selective payloads | Confirmed | Independent text/arguments/output revisions and byte counts travel in shape rows. The delayed history page does not overwrite a newer revision. Text, arguments, and output are fetched only when selected; closing output interest stops further output requests. The test reconstructs selected text from append parts and leaves reasoning/output absent from the shape wire. |
| Live head arrival while old history stays loaded | Confirmed | A newly created head item enters the 30-row tail while the already loaded 60 older rows remain visible. The anchor and row IDs stay stable. |
| Atomic React visibility for item/control/command updates | Confirmed | These entities share one tagged row collection. Both browser pages render only the old tuple or the new tuple from the single PostgreSQL transaction; no intermediate combination is observed. |
| Reconnect with same handle, no gaps/duplicates/regressions | Confirmed | A real page-1 live poll is aborted with `net::ERR_INTERNET_DISCONNECTED`; writes continue while offline. The browser resumes using the same Electric handle and offset, catches up both changed items, and observes monotonically increasing revisions. |
| Expired handle / `must-refetch` and bounded on-demand reset | Confirmed for the tested no-cache flow | Electric returns a 409 `must-refetch`. TanStack sends an 80-byte `offset=-1` request with no subset parameters, then reissues the three active tail/history subsets at 30 rows each. The browser retains 60 loaded history rows and the newer item revisions. `must-refetch-evidence.json` records the request sequence and UI state. |
| Electric restart and replication-slot/WAL observation | Confirmed in the single-service test | Electric 1.8 is stopped and restarted; both browser pages receive the next update. One logical slot is active with `wal_level=logical`; retained WAL measured 355,352 bytes in this run. Requests held against the deliberately stopped service briefly return 500 before the new service is ready; the collections retry and recover. |
| Authentication, multiple browsers, and two proxy instances | Confirmed in the test topology | The API rejects cross-conversation access and caller-supplied GET table, POST table, and conversation `WHERE` overrides. Both browsers receive updates while 63 requests alternate across two stateless proxy instances (32 and 31 requests), with no sticky state. |
| Sustained write/network amplification and persisted-cache recovery | Not established | Response rows/bytes, per-field payload parts, query plans, and one WAL measurement are recorded, but there is no sustained throughput benchmark or production retention policy. No browser persistence or collection tags are configured, so cold persisted-cache/tag recovery still needs a separate test. |

TanStack's current [Electric collection recovery notes](https://tanstack.com/db/latest/docs/collections/electric-collection#cleanup-and-resume-safety)
say a cold persisted resume that needs tag membership can request a full shape
snapshot even in on-demand mode. That persisted-cache/tag path is not enabled in
this spike; the bounded reset result above applies only to the tested
in-memory, active-subset flow. Do not infer a bounded cold-cache reset from the
30-row bootstrap result.

## Deliberate limits

- The test uses one PostgreSQL database, one Electric service at a time, one
  Python gateway, and two Python Electric proxy instances. It does not prove
  multi-region topology, proxy load balancing across machines, or production
  WAL retention bounds.
- Authentication is a small in-memory token-to-conversation map for the test.
  It proves that the proxy fixes conversation/table/column scope and rejects
  overrides; production identity and secret handling are out of scope.
- The event fixture and projection are intentionally small and local to this
  spike. This is not the production projector implementation and does not
  capture raw runner output.
- BuildBuddy test outputs are evidence for a particular invocation; they are
  not committed screenshots or performance baselines.
