# Agentplane Electric sync derisk spike

This folder is an isolated experiment for the conversation-view sync design in
[PR #7479](https://github.com/agentydragon/ducktape/pull/7479). It does not
change the Agentplane runner, application, database, or deployed behavior. The
projector consumes the existing durable `EventEntry` protocol and builds a small
PostgreSQL read model; a Python proxy authorizes Electric shape requests; a
React browser uses TanStack DB. Nothing here is connected to an application
runtime yet.

## Run the experiment

Run the browser E2E and focused Electric capacity tests through Bazel and
BuildBuddy. Their PostgreSQL, Electric, and Chromium containers require RBE:

```bash
nix develop --command bbr test \
  //x/agentplane_sync:e2e_test \
  //x/agentplane_sync:shape_capacity_test \
  --test_output=errors
```

The E2E target writes its HAR, screenshots, Playwright trace, SQL plans, request
measurements, and container logs to Bazel undeclared test outputs. The capacity
target writes `shape-capacity-evidence.json`, including shape handles, snapshot
rows/bytes, LRU expiry/reset responses, and Electric container memory samples.
After a run, retrieve artifacts from the BuildBuddy invocation:

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

| Component                                      | Pin                                                  | Source                                                                                                        |
| ---------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| PostgreSQL                                     | repository `postgres_18` OCI digest                  | `third_party/containers`                                                                                      |
| Electric standalone service for browser E2E    | 1.8.0, Linux amd64 OCI digest in `MODULE.bazel`      | [HTTP API](https://electric.ax/docs/sync/api/http), [configuration](https://electric.ax/docs/sync/api/config) |
| Electric standalone service for capacity probe | 1.8.1, exact deployment OCI digest in `MODULE.bazel` | [HTTP API](https://electric.ax/docs/sync/api/http), [configuration](https://electric.ax/docs/sync/api/config) |
| Electric TypeScript client                     | `@electric-sql/client` 1.5.28                        | `package.json` / `pnpm-lock.yaml`                                                                             |
| TanStack Electric collection                   | `@tanstack/electric-db-collection` 0.4.7             | `package.json` / `pnpm-lock.yaml`                                                                             |
| TanStack React DB                              | `@tanstack/react-db` 0.3.7                           | `package.json` / `pnpm-lock.yaml`                                                                             |

The old workspace pin `@tanstack/db` 0.9.2 is not used by this spike. The
selected React DB and Electric collection resolve the same `@tanstack/db` 0.8.7
core in the lockfile; importing them alongside the workspace's older core would
otherwise create incompatible collection types.

The view browser requests `log=changes_only`, starts its shared stream at
`offset=now`, and uses Electric subset snapshots for tail and history pages. The
adapter emits legacy `subset__*` GET parameters in this pin; the proxy validates
and forwards them with a 30-row maximum. A query-plan check separately guards
against bounded response sizes hiding an unbounded PostgreSQL scan. Payloads use
two ordinary Electric shapes: a follow-latest generation shape carries the
selected current body and future append rows, while a pinned revision shape has
server-derived manifest bounds and an on-demand snapshot.

The capacity probe uses the same pinned Electric 1.8.1 image as the current
Agentplane deployment. It seeds 1,000-row and 20,000-row histories, configures
`ELECTRIC_MAX_SHAPES=2`, creates three shapes, and records container cgroup
usage plus PID 1 RSS/high-water samples. After the periodic expiry cycle, it
requires the oldest handle to return 409 and a fresh snapshot to return every
row. The browser E2E still uses the repository's 1.8.0 image. A passing
409/reset probe establishes the configured handle lifecycle; these samples do
not by themselves establish a long-run server-memory bound.

## Evidence matrix

The original pushed implementation passed `//x/agentplane_sync:e2e_test` in
[RBE run 07ce30cf](https://app.buildbuddy.io/invocation/07ce30cf-7a96-410d-9fc1-83abd9ac0ec9).
That run predates the immutable-revision extension below. Its HAR, browser request journal, SQL
plans, screenshots, traces, service logs, and summarized measurements are
available as undeclared test outputs. This is prototype evidence from one test
environment, not a production load test.

| Gate                                                                                | Status                                       | Evidence or open question                                                                                                                                                                                                                                                                                                                                              |
| ----------------------------------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Projection idempotency, two workers racing one prefix, failed-batch rollback, retry | Confirmed                                    | `e2e-summary.json` records one writer, duplicate events for the losing worker, unchanged checkpoint/payload after rollback, and a successful retry.                                                                                                                                                                                                                    |
| Bounded SQL work                                                                    | Confirmed                                    | `query-plans.json` covers the 50,000-row tail and exclusive-before queries. The test requires `sync_view_row_tail` and at most 30 actual rows from each bounded index scan.                                                                                                                                                                                            |
| Exact bigint cursors and exclusive-before history                                   | Confirmed                                    | Python API and browser preserve `9007199254740993`; the loaded page has anchors strictly before the tail cursor. During the held page response, newer text and completion events commit, and the browser renders the newer revision with `complete` status.                                                                                                            |
| Initial bootstrap for 1,000 and 50,000 rows                                         | Confirmed                                    | Both real Electric snapshots load 30 rows. Captured response sizes were 19,092 bytes and 19,100 bytes respectively; the large history did not inflate the initial transfer. Shape rows omit text, arguments, output, reasoning, and content fields.                                                                                                                    |
| Scroll anchor pixel preservation                                                    | Confirmed                                    | `scroll-evidence.json`: anchor top 175.875px before loading, 1,135.875px before correction, correction 960px, and final displacement 0px.                                                                                                                                                                                                                              |
| Any-item and out-of-order updates; selective payloads                               | Partial                                      | The original run established selective loading on an after-cursor payload route, but that route did not prove an exact immutable body revision or bounded reopen. The current code replaces it with exact manifest references and immutable chunks; see the active revision experiment below.                                                                          |
| Exact body revision R while R+1 commits; stale selection/hydration callbacks        | Confirmed for pinned and follow-latest reads | [RBE run `05e6146f`](https://app.buildbuddy.io/invocation/05e6146f-ab09-4c01-adf9-645c5befe03c) held R's Electric snapshot until R+1 committed, then rendered exact R. A follow-latest selection rendered R+1 and then R+2. That run stopped later at the old manual-cleanup assertion, before the new lifecycle test.                                                 |
| Bounded exact-revision reconstruction after later same-generation appends           | Confirmed for the tested R prefix            | The same run reopened pinned R with its two-chunk manifest while the current reference had advanced. The request carried exact BIGINT generation and cursor values as decimal positional parameters; the shape constrained `chunk_index` to R's manifest count. This does not measure a long-run retention policy.                                                     |
| Follow-latest append after advancing the manifest                                   | Partial                                      | The browser rendered R+1, then the newly appended R+2 body from the same generation, with the exact current ref/revision. The 32-chunk transfer experiment later in the test was not reached by that run.                                                                                                                                                              |
| Closing selected-body interests releases collection rows and stops delivery         | In progress                                  | The prior RBE run exposed an unsafe manual `Collection.cleanup()` from the panel's React effect cleanup. Pinned TanStack DB 0.8.7 source shows that call is immediate; the replacement relies on no-subscriber GC and records collection size, subscribers, cleanup time, and Chromium heap after repeated reopen/close cycles. New RBE evidence is pending.           |
| Live head arrival while old history stays loaded                                    | Confirmed                                    | A newly created head item enters the 30-row tail while the already loaded 60 older rows remain visible. The anchor and row IDs stay stable.                                                                                                                                                                                                                            |
| Atomic React visibility for item/control/command updates                            | Confirmed                                    | These entities share one tagged row collection. Both browser pages render only the old tuple or the new tuple from the single PostgreSQL transaction; no intermediate combination is observed.                                                                                                                                                                         |
| Reconnect with same handle, no gaps/duplicates/regressions                          | Confirmed                                    | A real page-1 live poll is aborted with `net::ERR_INTERNET_DISCONNECTED`; writes continue while offline. The browser resumes using the same Electric handle and offset, catches up both changed items, and observes monotonically increasing revisions.                                                                                                                |
| Expired handle / `must-refetch` and bounded on-demand reset                         | Confirmed for the tested no-cache flow       | Electric returns a 409 `must-refetch`. TanStack sends an 80-byte `offset=-1` request with no subset parameters, then reissues the three active tail/history subsets at 30 rows each. The browser retains 60 loaded history rows and the newer item revisions. `must-refetch-evidence.json` records the request sequence and UI state.                                  |
| Electric restart and replication-slot/WAL observation                               | Confirmed in the single-service test         | Electric 1.8 is stopped and restarted; both browser pages receive the next update. One logical slot is active with `wal_level=logical`; retained WAL measured 366,192 bytes in this run. Requests held against the deliberately stopped service briefly return 500 before the new service is ready; the collections retry and recover.                                 |
| Configured shape-cap eviction and fresh-handle reset on Electric 1.8.1              | Confirmed                                    | RBE run [`b5fdc116`](https://app.buildbuddy.io/invocation/b5fdc116-9ae6-4742-acd2-eab63ce4979a): full snapshots returned 1k, 20k, and 1k rows; with `ELECTRIC_MAX_SHAPES=2`, the oldest handle returned 409 plus `must-refetch` after 75s, and a new handle reloaded all 1k rows. Logs show the expiry manager removing that handle.                                   |
| Electric process memory during the 1.8.1 capacity probe                             | Sampled; bounded growth unproven             | `shape-capacity-evidence.json` records PID 1 RSS from 317,396 KiB at readiness to 313,380 KiB after the 20k-row full shape, 301,764 KiB after expiry, and 302,072 KiB after reset; peak RSS was 319,308 KiB. cgroup usage ranged from 258.8 MB after expiry to 272.4 MB at readiness. This single full-shape run is not evidence of a fixed-active-shape memory bound. |
| Authentication, multiple browsers, and two proxy instances                          | Confirmed in the test topology               | The API rejects cross-conversation access and caller-supplied GET table, POST table, and conversation `WHERE` overrides. Both browsers receive updates while 62 requests alternate across two stateless proxy instances (31 each), with no sticky state.                                                                                                               |
| Sustained write/network amplification and persisted-cache recovery                  | Not established                              | Response rows/bytes, per-field payload parts, query plans, and one WAL measurement are recorded, but there is no sustained throughput benchmark or production retention policy. No browser persistence or collection tags are configured, so cold persisted-cache/tag recovery still needs a separate test.                                                            |

TanStack's current [Electric collection recovery notes](https://tanstack.com/db/latest/docs/collections/electric-collection#cleanup-and-resume-safety)
say a cold persisted resume that needs tag membership can request a full shape
snapshot even in on-demand mode. That persisted-cache/tag path is not enabled in
this spike; the bounded reset result above applies only to the tested
in-memory, active-subset flow. Do not infer a bounded cold-cache reset from the
30-row bootstrap result.

### Immutable body revision experiment

The current working extension stores independent text, argument, output, and
reasoning revisions. A payload reference names its conversation, item, field,
source, generation, and revision. An immutable manifest gives the exact
revision's presence, chunk count, byte count, and source cursor. Appends add
immutable indexed chunks within a generation; an authoritative completion or
replacement starts a new generation, including an empty-but-present value.
The projected item row changes only the affected field's latest reference.

`GET /api/payloads/{conversation}/{payloadRef}` reads that exact manifest and
returns 410 when it is unavailable. The authenticated proxy derives the
Electric table, columns, and fixed owner predicate from the manifest. The
`chunks` shape is for a field that follows its latest reference: it is scoped to
one immutable generation, starts at the current WAL offset, and streams later
append rows once. The browser renders only chunks below the selected manifest's
count and cursor, and waits for the complete byte count before showing a body.
The `revision` shape is pinned to an exact manifest: its server-owned
`WHERE` includes `chunk_index < chunk_count` and `source_cursor <= source_cursor`,
uses `log=changes_only&offset=now`, and rejects client filters, limits, and
ordering. This lets an old reference reopen through a bounded on-demand
snapshot even after more chunks were appended to the same generation.

The browser supports a pin action and a `?payloadRef=...` deep link to inspect a
specific immutable revision. A newer reference never substitutes for a pinned
one. This uses ordinary Electric snapshots and change streams; the spike adds
no payload replay protocol. The follow-latest shape transfers the complete
current body because that body is selected, then streams append chunks without
re-requesting the full prefix. The pinned shape transfers only the requested
manifest prefix. In the last inspected RBE run, R's held snapshot returned two
chunks even though R+1 had become current, and the follow-latest selection then
rendered R+1 and R+2. The test verifies the forwarded positional BIGINT
parameters and the `EXPLAIN ANALYZE` range scan. That run later failed at the
manual-cleanup assertion, so it does not count as a passing full E2E invocation.

The pinned `@tanstack/db` 0.8.7 source confirms that `Collection.cleanup()`
performs immediate cleanup regardless of active subscribers. Its automatic GC
instead waits for the subscriber count to reach zero; `useLiveQuery` in pinned
React DB 0.3.7 gives its derived query a 1 ms GC interval. Payload source
collections now also use a short positive GC interval and wait for the derived
query's source subscription to retire. The new browser scenario appends 64
8 KiB chunks while the interest is closed, checks there are no requests or
rendered body for that new ref, then reopens and renders the complete current
value four times. It requires each source collection to be `cleaned-up` with
zero rows and subscribers. Chromium CDP heap readings after forced GC are
recorded and gated against a 2 MiB retained-heap range; this is a small repeated
cycle probe, not a sustained memory bound.

The production projector may choose another physical layout; it must preserve
the exact-reference, immutable-prefix, per-field, and transaction semantics.

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
- Long-run browser heap bounds/virtualization and Electric server RSS/retained
  heap across fixed active subscriptions, unrelated history growth, shape
  churn/expiry, resets, restarts, and slow readers remain unproven. The 1.8.1
  capacity probe measures a single 1k/20k/1k full-shape sequence; replication
  buffers, shape caches/history, backpressure, and payload retention policy
  remain server-side adoption gates.
- BuildBuddy test outputs are evidence for a particular invocation; they are
  not committed screenshots or performance baselines.
