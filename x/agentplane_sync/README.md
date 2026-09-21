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

Status below reflects the latest completed RBE runs while the draft is being
iterated. `Not demonstrated` means the browser has not completed the gate; it
does not mean the behavior passed.

| Gate | Status | Evidence or open question |
| --- | --- | --- |
| Projection idempotency, two workers racing one prefix, failed-batch rollback, retry | Confirmed before browser bootstrap | PostgreSQL test asserts one writer, duplicate digests, unchanged checkpoint/payload on rollback, and a successful retry. |
| Bounded SQL work | Confirmed before browser bootstrap | The seeded 50,000-row history's tail and exclusive-before plans must use `sync_view_row_tail` and return at most 30 actual rows from the index scan. The JSON plans are undeclared outputs. |
| Exact bigint cursors and exclusive-before API page | Confirmed at Python API boundary | History API asserts the decimal string `9007199254740993` survives and `anchor < before` is exclusive. Browser verification is still required. |
| API conversation authorization and caller shape overrides | Confirmed at Python proxy boundary | Tests reject an unscoped conversation, GET table override, POST table override, and attempt to add another conversation in subset `WHERE`. The real browser receives scoped Electric rows; multi-listener authorization remains pending. |
| Real Electric subset snapshot and initial 30-row browser tail | Confirmed for initial bootstrap only | RBE invocation `a2fd8105-9c88-4c9d-b3ed-5bc9fad9ad14` saved `alpha-small-tail.png` and `alpha-large-tail.png`; both passed 30-row client-state and shape-row bounds, byte-size scaling, and omitted-payload checks. The small fixture also passed browser bigint `9007199254740993`. This does not establish history or reset bounds. |
| Exclusive-before browser history page and late update racing its fetch | Confirmed in RBE invocation `953f81e5-e87c-4435-8010-13727c29d4bf` | The delayed Electric subset response contained 30 rows with anchors strictly older than the tail cursor. While it was held, the test committed newer text and completion revisions; after release, the browser rendered the target at the new revision and `complete` status. The HAR-style request journal reports the 30-row snapshot and two-row change batch. |
| Scroll anchor pixel preservation | Pending rerun after an evidence bug | The browser measured a 960px pre-correction shift after adding 30 rows, then adjusted `scrollTop` by the same amount. The earlier assertion recorded the pre-correction position; the next run records both values and still requires the final displacement to be at most 1px. |
| Any-item updates, selective payload loading/eviction, and append/replacement cost | Not demonstrated yet | Body chunks are stored separately as append/replace payload parts; shape rows contain per-field revisions and byte counts only. Browser interest tests have not run yet. |
| Live head arrivals with older loaded rows retained | Not demonstrated yet | Browser assertions require a newly created head item to appear while already loaded older rows stay present. The late text update so far targets an existing item. |
| Atomic React visibility for item/control/command updates in one Postgres transaction | Not demonstrated yet | All three entities share one tagged relation; the browser records rendered tuples and rejects intermediate mixed states. PostgreSQL transaction atomicity alone is not treated as proof. |
| Reconnect with same handle, no gaps/duplicates/regressions | Not demonstrated yet | The browser goes offline while writes continue, then checks resumed handle/offset, both listeners, and monotone revisions. |
| Expired handle / must-refetch and bounded reset | Not demonstrated yet; likely adoption blocker | The browser corrupts a live handle and records the actual reset request and rows. Current TanStack DB documentation says recovery can request a full snapshot in on-demand mode. The measured reset size must decide whether bounded recovery is acceptable. |
| Electric service restart and replication-slot/WAL observations | Not demonstrated yet | The test restarts the pinned service and checks recovery; it also records logical slots and retained WAL bytes if the complete browser run reaches teardown. A single-process test does not establish deployment topology or WAL operations policy. |
| Two browsers and two stateless proxy instances | Not demonstrated yet | Requests alternate between proxy instances without sticky state; both pages must observe the same writes. |

TanStack's current [Electric collection recovery notes](https://tanstack.com/db/latest/docs/collections/electric-collection#cleanup-and-resume-safety)
say a reset can replace cached rows from a full snapshot even in on-demand
mode. This spike intentionally measures that behavior instead of inferring
bounded recovery from the first-page limit. Persistent local storage is not
enabled here, so cold persisted-cache recovery is not covered.

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
