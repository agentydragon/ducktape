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

| Component                    | Pin                                             | Source                                                      |
| ---------------------------- | ----------------------------------------------- | ----------------------------------------------------------- |
| PostgreSQL                   | repository `postgres_18` OCI digest             | `third_party/containers`                                    |
| Electric standalone service  | 1.8.0, Linux amd64 OCI digest in `MODULE.bazel` | [Electric HTTP API](https://electric.ax/docs/sync/api/http) |
| Electric TypeScript client   | `@electric-sql/client` 1.5.28                   | `package.json` / `pnpm-lock.yaml`                           |
| TanStack Electric collection | `@tanstack/electric-db-collection` 0.4.7        | `package.json` / `pnpm-lock.yaml`                           |
| TanStack React DB            | `@tanstack/react-db` 0.3.7                      | `package.json` / `pnpm-lock.yaml`                           |

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

The original pushed implementation passed `//x/agentplane_sync:e2e_test` in
[RBE run 07ce30cf](https://app.buildbuddy.io/invocation/07ce30cf-7a96-410d-9fc1-83abd9ac0ec9).
That run predates the immutable-revision extension below. Its HAR, browser request journal, SQL
plans, screenshots, traces, service logs, and summarized measurements are
available as undeclared test outputs. This is prototype evidence from one test
environment, not a production load test.

| Gate                                                                                | Status                                 | Evidence or open question                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------------------------------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Projection idempotency, two workers racing one prefix, failed-batch rollback, retry | Confirmed                              | `e2e-summary.json` records one writer, duplicate events for the losing worker, unchanged checkpoint/payload after rollback, and a successful retry.                                                                                                                                                                                                                              |
| Bounded SQL work                                                                    | Confirmed                              | `query-plans.json` covers the 50,000-row tail and exclusive-before queries. The test requires `sync_view_row_tail` and at most 30 actual rows from each bounded index scan.                                                                                                                                                                                                      |
| Exact bigint cursors and exclusive-before history                                   | Confirmed                              | Python API and browser preserve `9007199254740993`; the loaded page has anchors strictly before the tail cursor. During the held page response, newer text and completion events commit, and the browser renders the newer revision with `complete` status.                                                                                                                      |
| Initial bootstrap for 1,000 and 50,000 rows                                         | Confirmed                              | Both real Electric snapshots load 30 rows. Captured response sizes were 19,092 bytes and 19,100 bytes respectively; the large history did not inflate the initial transfer. Shape rows omit text, arguments, output, reasoning, and content fields.                                                                                                                              |
| Scroll anchor pixel preservation                                                    | Confirmed                              | `scroll-evidence.json`: anchor top 175.875px before loading, 1,135.875px before correction, correction 960px, and final displacement 0px.                                                                                                                                                                                                                                        |
| Any-item and out-of-order updates; selective payloads                               | Partial                                | The original run established selective loading on an after-cursor payload route, but that route did not prove an exact immutable body revision or bounded reopen. The current code replaces it with exact manifest references and immutable chunks; see the active revision experiment below. |
| Exact body revision R while R+1 commits; stale selection/hydration callbacks       | In progress                            | RBE run `109c2098` captured the exact R snapshot and observed the UI stay empty/hydrating while R+1 became current, then rendered complete R and R+1 values. That run stopped at an aggregate request-metrics assertion before later gates; the corrected assertion still needs an RBE rerun. |
| Bounded exact-revision reconstruction across superseded generations                | Not established                        | Small/large generation probes and query plans are added, but an eager snapshot is scoped to the whole append generation. Prefix filtering protects rendered revision R; it does not prove a reopen of old R avoids transferring later chunks from that same generation. The test must measure bytes/rows for that case. |
| Closing selected-body interests releases collection rows and stops delivery         | In progress                            | The implementation calls TanStack `Collection.cleanup()` and records post-cleanup size/subscribers/status; the new close/reopen browser assertions have not yet completed in RBE. Heap reclamation is not established by collection cleanup. |
| Live head arrival while old history stays loaded                                    | Confirmed                              | A newly created head item enters the 30-row tail while the already loaded 60 older rows remain visible. The anchor and row IDs stay stable.                                                                                                                                                                                                                                      |
| Atomic React visibility for item/control/command updates                            | Confirmed                              | These entities share one tagged row collection. Both browser pages render only the old tuple or the new tuple from the single PostgreSQL transaction; no intermediate combination is observed.                                                                                                                                                                                   |
| Reconnect with same handle, no gaps/duplicates/regressions                          | Confirmed                              | A real page-1 live poll is aborted with `net::ERR_INTERNET_DISCONNECTED`; writes continue while offline. The browser resumes using the same Electric handle and offset, catches up both changed items, and observes monotonically increasing revisions.                                                                                                                          |
| Expired handle / `must-refetch` and bounded on-demand reset                         | Confirmed for the tested no-cache flow | Electric returns a 409 `must-refetch`. TanStack sends an 80-byte `offset=-1` request with no subset parameters, then reissues the three active tail/history subsets at 30 rows each. The browser retains 60 loaded history rows and the newer item revisions. `must-refetch-evidence.json` records the request sequence and UI state.                                            |
| Electric restart and replication-slot/WAL observation                               | Confirmed in the single-service test   | Electric 1.8 is stopped and restarted; both browser pages receive the next update. One logical slot is active with `wal_level=logical`; retained WAL measured 366,192 bytes in this run. Requests held against the deliberately stopped service briefly return 500 before the new service is ready; the collections retry and recover.                                           |
| Authentication, multiple browsers, and two proxy instances                          | Confirmed in the test topology         | The API rejects cross-conversation access and caller-supplied GET table, POST table, and conversation `WHERE` overrides. Both browsers receive updates while 62 requests alternate across two stateless proxy instances (31 each), with no sticky state.                                                                                                                         |
| Sustained write/network amplification and persisted-cache recovery                  | Not established                        | Response rows/bytes, per-field payload parts, query plans, and one WAL measurement are recorded, but there is no sustained throughput benchmark or production retention policy. No browser persistence or collection tags are configured, so cold persisted-cache/tag recovery still needs a separate test.                                                                      |

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
returns 410 when it is unavailable. The authenticated Electric proxy derives
the chunk shape's table, columns, and owner/source/generation predicate from
the manifest. The pinned eager adapter's initial content request uses
`log=full&offset=-1`; the proxy forces this only for the generation-scoped
chunk shape and keeps the mutable view on `log=changes_only`. The browser
accepts only contiguous chunk indexes below the exact manifest's `chunkCount`
and renders the body only after the byte total matches. A newer reference
never substitutes for a selected older reference.

This design avoids storing/retransmitting a growing full text value on each
append, but its initial full-generation snapshot may include chunks appended
after a selected older revision. Exact rendering is demonstrated by the active
test, while bounded bytes for a stale-revision reopen remain an adoption gate.
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
- Browser heap bounds/virtualization and Electric server RSS/retained heap across
  history growth, resets, restarts, and slow readers are unmeasured. Replication
  buffers, shape caches/history, backpressure, and payload retention policy are
  still server-side adoption gates.
- BuildBuddy test outputs are evidence for a particular invocation; they are
  not committed screenshots or performance baselines.
