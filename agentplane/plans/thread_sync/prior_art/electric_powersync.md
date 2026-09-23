# Prior art, verified: ElectricSQL and PowerSync

Read 2026-09-23 from the docs' source repos: `electric-sql/electric` @ `bb39742` (2026-09-09),
`powersync-ja/powersync-docs` @ `fe0415e` (2026-09-22) and `TanStack/db` @ `6907244` (2026-09-23), with
the relevant Electric server and TanStack DB source checked where the docs were silent. Each cited
URL resolves (HTTP 200). `electric-sql.com` now answers 301 → `electric.ax`, and Electric's docs moved
under `/docs/sync/…`. Evidence marked `inferred` is my reading, not something a document states.

## A. ElectricSQL

- **Latest:** `electricsql/electric:1.8.1`, Docker Hub 2026-09-07 (canary 2026-09-09). This is what we
  deploy. `@electric-sql/client` 1.5.28 (2026-09-09), `@tanstack/electric-db-collection` 0.4.10
  (2026-09-14). License Apache-2.0 for both the server and the client.
- **Company:** "Electric is joining Databricks" (blog, 2026-08-11): "Everything Electric has previously
  open sourced stays open source", and "Electric Cloud is winding down." Releases have continued
  since (1.8.0 on 09-01, 1.8.1 on 09-07). The roadmap is now tied to Neon/Lakebase, which is a
  maturity risk to weigh against O4 and D3.

### Protocol model

- A shape is `(table, where+params, columns, replica, log mode)`. Server source:
  `comparable/1` = `{table, pk, where, selected_columns, …, replica, log_mode}`. It is immutable:
  "Shape definitions are currently immutable."
- A shape is one server-side log, addressed by `offset` and `handle`. It is shared by every reader
  whose definition matches, resumed with `offset=<last>&handle=<H>`, and followed live by long-poll or
  SSE (`live_sse=true`).
- `log=changes_only` "skips creating an initial snapshot". `offset=now` returns the current
  continuation offset and no data.
- **Subset snapshots** (since 1.1.12) are one-shot reads of a narrower slice of an existing shape,
  sent as POST with `{where, params, order_by, limit, offset}` (GET `subset__*` is legacy). The server
  combines them as `WHERE {main_shape_where} AND ({subset_where})`. It runs them directly against
  Postgres with "same columns as the base shape" (`querying.ex`), because a subset has no `columns`
  parameter. Each response ends with `snapshot-end {xmin, xmax, xip_list}`, which the client uses to
  skip live-log changes the snapshot already contains.
- **So one stable shape per thread can serve a narrow tail and then further pages.** Use
  `changes_only`, then `offset=now`, then a subset (`entity_index DESC LIMIT n`), then live. Scrolling
  back is another subset for the range not yet held. The shape's predicate never moves.
- **The live log still carries every change in the whole shape.** It is not narrowed to the subsets
  a client has loaded. TanStack: "On-demand streams can observe updates outside their loaded subsets;
  unknown partial rows are ignored."
- **TanStack DB `syncMode`:** `eager` | `on-demand` | `progressive`. `on-demand` maps each live
  query's `loadSubset(where, orderBy, limit, cursor)` to `stream.requestSnapshot`. When the window
  grows it sends a cursor (`whereFrom` = rows past the last held boundary, plus a `whereCurrent`
  tie query), so paging an ordered or limited query fetches only the new rows. **A moved window with
  a different `where` re-fetches the overlap.** `DeduplicatedLoadSubset` "Deduplicates exact
  canonical demands without inferring broader coverage". Before each subset it also forces the live
  poll to reconnect.
- **Auth pattern:** a proxy pins `table`, `where` and `queryable_columns` (new in 1.6.10). A client
  subset "can only narrow results, never widen them", and it may not contain subqueries.
- **Operations:**
  - Storage is `MEMORY` or `FAST_FILE` only, holding shape logs on local disk with SQLite metadata.
    There is **no Postgres- or object-storage shape backend**.
  - Electric runs as a "single active instance" per replication stream, held by an advisory lock.
    Read scaling means a CDN or caching proxy with request collapsing, or separate instances that
    each have their own slot and storage behind sticky sessions.
  - `ELECTRIC_MAX_SHAPES` defaults to unlimited. The limit of 1024 is our own setting.

**Design scored below:** one `changes_only` shape per thread for entities, one per thread per content
`field` over the chunk table (for P8), and one control shape per thread. The tail and every scrolled-
back page are subset snapshots. All live traffic flows over the shared per-thread logs through SSE,
and our proxy authorizes each request and caps `limit`.

| ID  | V   | Reason                                                                                                                                | Evidence                                                                                                                                          |
| --- | --- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| C2  | +   | Proxy pins main `where` to the authorized thread. Subsets only narrow. Proxy can require/cap `limit` (unbounded clause).              | auth: "subset queries can only narrow results, never widen them"; "validate the user credentials and set the shape parameters server-side"        |
| P1  | +   | Tail = one subset `ORDER BY entity_index DESC LIMIT n` on the thread shape.                                                           | http: "In `changes_only` mode, you can use subset snapshots … to fetch specific portions of data on-demand"                                       |
| P2  | +   | Live log of the thread shape (SSE or long-poll).                                                                                      | http § Live mode                                                                                                                                  |
| P3  | +   | Any change to a row matching the shape `where` enters its log, whatever its index.                                                    | http: "you're getting the ones that affect the data in your Shape"                                                                                |
| P4  | +   | Each step back = one subset with `limit` + `order_by`, bounded.                                                                       | http: subset POST body `limit`, `offset`, "`order_by` … (required when using limit/offset)"                                                       |
| P5  | ~   | Additive subsets never withdraw rows; but a 409 (eviction, schema change, slot loss) makes the stock client discard the shape's rows. | shapes: "The client discards its local copy of the shape data." (on 409/`must-refetch`)                                                           |
| P6  | +   | Resume the log from `offset`+`handle`; subsets already applied stay held.                                                             | OpenAPI `offset`: "set the `offset` to the last offset you have already received, to continue syncing"                                            |
| P7  | +   | Epoch in the shape `where`; proxy answers 410 for a stale epoch (as deployed).                                                        | inferred (proxy-owned)                                                                                                                            |
| P8  | +   | Per-field content shapes, client subscribes to the fields it wants; each is live. A subset cannot pick columns.                       | querying.ex: "When querying a subset, we select same columns as the base shape"                                                                   |
| P9  | ~   | Engine-neutral; TanStack `awaitTxId` matches txids, lost-reply reconcile is app-level.                                                | tanstack electric-collection § `awaitTxId`; inferred                                                                                              |
| S1  | ~   | Entity and chunk shapes have independent logs; no documented cross-shape atomicity, so the `PayloadRef` extent is still needed.       | inferred (no consistency statement across shapes in docs)                                                                                         |
| S2  | +   | Client orders by `entity_index`.                                                                                                      | inferred                                                                                                                                          |
| S3  | +   | `up-to-date` per shape, `snapshot-end` per subset.                                                                                    | http: `up-to-date` "indicates that the client has all the data that the server was aware of"; `snapshot-end` "marks the end of a subset snapshot" |
| S4  | +   | Held row's revisions always arrive (whole-thread log); no recency filter anywhere.                                                    | http § Shape Log (as P3)                                                                                                                          |
| E1  | +   | Per shape: `offset=now` + subset + live = 3, constant; ~4–5 shapes per thread.                                                        | inferred from http § Starting from 'now'                                                                                                          |
| E2  | +   | Snapshot bytes = the subset; no history replay (changes_only).                                                                        | http: `changes_only` "the server skips creating an initial snapshot"                                                                              |
| E3  | +   | With `live_sse=true` ≈0 requests; long-poll costs one request per live batch per shape.                                               | http: SSE "Fewer HTTP requests - the client doesn't need to reconnect after each message"                                                         |
| E4  | +   | Subset for the unheld range only (raw client, or TanStack cursor paging).                                                             | electric.ts: "One for whereFrom (rows > cursor) with limit"                                                                                       |
| E5  | ~   | Held rows never re-sent; but live log also carries changes for rows outside the window, and a 409 forces re-snapshotting held rows.   | tanstack: "On-demand streams can observe updates outside their loaded subsets"                                                                    |
| E6  | +   | The live log is followed by long poll or SSE (`live_sse=true`); nothing is re-requested on a timer.                                   | http § Live mode                                                                                                                                  |
| O1  | +   | State is per thread (≈4 + fields shapes), shared by all readers, independent of window; subsets are stateless PG queries.             | shapes: "Many clients can sync the same shape."; querying.ex `query_subset`                                                                       |
| O2  | ~   | Single active Electric per slot; scale reads via CDN/collapsing; multi-instance needs separate storage + sticky sessions.             | upgrading: "Electric is designed to run as a **single active instance** per replication stream"                                                   |
| O3  | +   | Proxy sees every shape and subset; server spans carry subset params, rows, bytes.                                                     | CHANGELOG 29a8cde: "capture POST body params as request telemetry attributes"                                                                     |
| O4  | +   | Already deployed; no new engine.                                                                                                      | deployed                                                                                                                                          |
| D1  | +   | Scroll = subset of unheld range; overlap never requested; held rows' changes arrive via the same log. Dropping 151–200 is local.      | http § Subset snapshots; TanStack caveat: a moved `where` re-fetches (subset-dedupe.ts)                                                           |
| D2  | +   | Streaming (live log) and on-demand (subset) are the same shape.                                                                       | inferred                                                                                                                                          |
| D3  | +   | Stepwise: switch the entity shape to changes_only+subsets first, then content, then drop pages/rotation.                              | inferred                                                                                                                                          |

### Operating it

1. **Scaling:** there is no Postgres- or object-storage shape backend.
   `ELECTRIC_STORAGE` ∈ {`MEMORY`, `FAST_FILE`}. Shared NFS/EFS storage is supported, with
   `ELECTRIC_SHAPE_DB_EXCLUSIVE_MODE=true`.
2. **Company status:** Electric is now at Databricks, and Electric Cloud is winding down. A lead,
   out of scope here: Electric now also ships **Durable Streams**, an Apache-2.0, append-only,
   offset-addressed HTTP stream protocol aimed at "agent loops". It is not a Postgres sync engine.

## B. PowerSync

- **Latest:** `journeyapps/powersync-service:1.26.1` (Docker Hub 2026-09-14). `@powersync/web` 2.4.1
  (2026-09-23), which runs SQLite in the browser.
- **License:** the service is FSL-1.1-ALv2. It is source-available with a "Competing Use" exclusion,
  and each release becomes Apache-2.0 "on the second anniversary". The client SDKs are Apache-2.0.
- **Maturity:** Open Edition GA. Postgres source GA. **Sync Streams GA**; Sync Rules are now "Legacy".
  Postgres bucket storage GA. Storage v4 (incremental reprocessing, S3) is Beta.

### Protocol model

- **Streams.** Server YAML defines Sync Streams, SQL-like queries that "select the tables and columns
  to sync". A client subscribes at runtime with **subscription parameters**
  (`db.syncStream('s', {thread, page}).subscribe()`). Several subscriptions to one stream with
  different values need no reconnect.
- **Buckets.** Each stream compiles to buckets, "one bucket for each unique value of its filter".
  They are precomputed at replication time, cover all data rather than only subscribed data, and are
  shared by every client with the same values. A bucket "is always synced as a whole".
- **Parameters are equality-only.** "parameter-based filtering is limited to equality checks (`=`,
  `IN`, `IS NULL`) — range operators like `>`, `<`, `>=`, or `<=` are not supported on parameters."
  A client cannot ask for `entity_index BETWEEN 50 AND 150`. It can ask for `page IN [1,2]`, given
  a stored page column (or a row-side expression; the docs use
  `substring(updated_at,1,10) = subscription.parameter('date')`).
- **Wire protocol.** There is one long-lived HTTP stream or WebSocket per client. The client sends
  "A list of current buckets that the client has, and the latest operation ID in each". The server
  streams checkpoint → per-bucket ops from those IDs → checkpoint complete, and resumes from there
  after any interruption.
- **Consistency.** Per-bucket checksums are checked at every checkpoint, and a mismatch deletes and
  re-downloads the bucket. The client applies data only at complete checkpoints, and "Different
  tables and buckets are all included in the same consistent checkpoint".
- **Priorities (0–3).** Higher-priority buckets are applied before the whole checkpoint completes.
  This is how a tail could beat history (P1/S3).
- **Unsubscribe.** "Data continues syncing for the TTL duration, then is removed". The default TTL is
  24 h.
- **Bucket history.** A bucket is an op history (`PUT`/`REMOVE`). A new client downloads everything
  since the last compaction, which a daily or hourly `compact` job produces. Of the MOVE step: "This does
  not reduce the number of operations to download".
- **Auth.** The app's `fetchCredentials()` returns a JWT and the PowerSync URL. The **browser then
  talks to the PowerSync Service directly**, and rows are scoped by `auth.*` claims and subqueries in
  stream SQL. This is Electric's "gatekeeper" pattern: the app authorizes at token issuance, not per
  read.

**Design scored below.** Two streams, both keyed by `(thread, page)` from a stored page column and
gated by an `auth.user_id()` subquery on thread membership:

- `entities`
- `content` with `field = subscription.parameter('field')` (P8)

A thread-meta stream carries `latest_page` and `view_state`.

| ID  | V   | Reason                                                                                                                                                            | Evidence                                                                                                                                                                 |
| --- | --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| C2  | −   | Letter violated: browser holds a bearer JWT and connects to the service (own subdomain). Spirit ~: app decides at token issue; enforcement moves into stream SQL. | auth: `fetchCredentials()` "must return a JWT as well the endpoint URL for the PowerSync Service"; deploy: "required to host the API container on a dedicated subdomain" |
| P1  | +   | Subscribe latest page(s), optionally at priority 1.                                                                                                               | streams/client-usage § Priority Override                                                                                                                                 |
| P2  | +   | Ops stream over the open connection.                                                                                                                              | protocol § Protocol                                                                                                                                                      |
| P3  | +   | Any change to a row in a bucket appends an op to it.                                                                                                              | service: "that change will be appended to the operation history in that bucket"                                                                                          |
| P4  | +   | One page bucket per step. Bounded by 1,000 buckets/connection (pages × fields).                                                                                   | limits: "Synced buckets per user … Maximum: 1,000 by default"                                                                                                            |
| P5  | +   | Local SQLite changes only at checkpoints; rows leave only on REMOVE/TTL. Rare exception: checksum failure drops a bucket.                                         | protocol: "If a checksum validation fails on the client, the client will delete the bucket"                                                                              |
| P6  | +   | Resume from last op ID per bucket; local DB persists.                                                                                                             | protocol: "the client will initiate a new session, resuming from the last point"                                                                                         |
| P7  | ~   | No refusal primitive: a stale-epoch subscription just matches nothing (empty, not 410).                                                                           | inferred                                                                                                                                                                 |
| P8  | +   | `field` as an equality subscription parameter (or column selection per stream); each field bucket is live.                                                        | streams overview: "selects the tables and columns to sync"                                                                                                               |
| P9  | ~   | Engine-neutral if commands bypass PowerSync writes; if they use its upload queue, a lost reply stalls _all_ downloads.                                            | consistency: "While mutations are present in the upload queue, the client does not advance to a new checkpoint."                                                         |
| S1  | +   | Entity and chunk rows committed together land in the same checkpoint (within one priority).                                                                       | consistency: "Different tables and buckets are all included in the same consistent checkpoint"                                                                           |
| S2  | +   | Local SQL `ORDER BY entity_index`.                                                                                                                                | inferred                                                                                                                                                                 |
| S3  | +   | Per subscription `hasSynced` / `waitForFirstSync()`; checkpoint-complete per session.                                                                             | client-usage: `if (!stream?.subscription.hasSynced)`                                                                                                                     |
| S4  | +   | Held bucket receives every op.                                                                                                                                    | protocol § Protocol                                                                                                                                                      |
| E1  | +   | One JWT fetch + one streaming connection; subscribing adds no connection.                                                                                         | migrate-to-sync-streams: "subscribe to the same stream several times with different values, without reconnecting"                                                        |
| E2  | ~   | Page-bounded, but a new reader downloads every op since last compaction, so a heavily revised tail page costs revisions, not rows.                                | compacting: "this history can grow large, causing new clients to potentially take a long time to download"                                                               |
| E3  | +   | Pushed on the open stream.                                                                                                                                        | protocol § Protocol                                                                                                                                                      |
| E4  | +   | New page = new bucket only.                                                                                                                                       | protocol: bucket "always synced as a whole"                                                                                                                              |
| E5  | +   | Resume by op ID; checksums revalidate without retransfer. Unsubscribed pages keep syncing for TTL (waste, 24 h default).                                          | client-usage: "Data continues syncing for the TTL duration"                                                                                                              |
| E6  | +   | One long-lived HTTP stream or WebSocket per client.                                                                                                               | protocol § Protocol                                                                                                                                                      |
| O1  | +   | Buckets deduplicated across readers; per-connection state is the bucket list. Server stores all buckets for all data (not per reader).                            | service: "deduplicates data that is shared between different users"                                                                                                      |
| O2  | +   | 1 replication process + N API containers over shared bucket storage (Postgres GA or MongoDB). ~100–200 connections per API container.                             | deploy: "2+ PowerSync API containers"; "Only one process can replicate at a time"                                                                                        |
| O3  | ~   | Per-connection logs (`rid`, bucket counts), Diagnostics API; no per-row delivery log.                                                                             | log-reference: "`buckets` counts the user's buckets"                                                                                                                     |
| O4  | −   | New engine: service + bucket-storage DB + compact cron + JWKS + WASM SQLite client; replaces Electric and the proxy.                                              | deploy § Production                                                                                                                                                      |
| D1  | +   | At page granularity: overlapping buckets resume from held op IDs; arbitrary ranges impossible (equality-only params).                                             | sync-data-by-time (quote above); protocol "latest operation ID in each"                                                                                                  |
| D2  | +   | Streamed and on-demand bodies are both just subscriptions.                                                                                                        | inferred                                                                                                                                                                 |
| D3  | −   | Engine swap; TanStack DB has a PowerSync collection, so the query layer could survive, but server, auth and wire all change at once.                              | tanstack powersync-collection § On-demand Sync Mode                                                                                                                      |

### Answers to the specific questions

- **Runtime parameters:** yes, via subscription parameters (GA). **Index range:** no; only equality or
  `IN` over a precomputed page key.
- **Moving without re-downloading (D1):** yes at page granularity. Buckets are identified by
  parameter values, and the client reports its op ID per bucket.
- **Partial window in browser SQLite:** yes. Only subscribed buckets are held. Paging back means
  subscribing to page `k-1`.
- **C2:** the model is "app issues a token whose claims scope the buckets", plus server-side SQL
  filters. It meets the app-decides clause at token issue, and the pull-volume clause through the
  bucket limit. It fails "no browser reaches a sync engine directly", and authorization lives in
  PowerSync's YAML rather than the app. Revocation waits for token expiry. A per-request authorizing
  proxy would have to parse the WebSocket or HTTP-stream sync request; that is not documented
  (inferred).
- **Self-hosting:** Postgres needs logical replication and a `powersync` publication. Bucket storage
  is MongoDB or Postgres, and S3 is Beta.

## Sources

- https://electric.ax/docs/sync/api/http (HTTP API, subset snapshots, SSE, log modes)
- https://electric.ax/docs/sync/guides/shapes (queryable_columns, immutability, eviction, 409 causes)
- https://electric.ax/docs/sync/api/config (`ELECTRIC_MAX_SHAPES`, `ELECTRIC_STORAGE`)
- https://electric.ax/docs/sync/guides/upgrading (single active instance, shared vs separate storage)
- https://electric.ax/docs/sync/guides/auth (proxy auth, WHERE combination rules)
- https://electric.ax/docs/sync/api/clients/typescript (`requestSnapshot`, `snapshot-end`)
- https://electric.ax/blog/2026/08/11/electric-joining-databricks
- `electric-sql/electric` `website/electric-api.yaml`, `packages/sync-service/CHANGELOG.md`,
  `lib/electric/shapes/{shape.ex,querying.ex,api/params.ex}` @ `bb39742`
- https://hub.docker.com/r/electricsql/electric/tags, npm `@electric-sql/client`
- https://tanstack.com/db/latest/docs/collections/electric-collection; `TanStack/db`
  `packages/electric-db-collection/src/electric.ts`, `packages/db/src/query/subset-dedupe.ts` @ `6907244`
- https://tanstack.com/blog/tanstack-db-0.5-query-driven-sync (Nov 2025: "only fetches the delta").
  This is superseded by the exact-key dedupe in current source.
- https://docs.powersync.com/sync/streams/overview, /sync/streams/parameters, /sync/streams/client-usage
- https://docs.powersync.com/sync/advanced/sync-data-by-time (equality-only parameters)
- https://docs.powersync.com/sync/supported-sql
- https://docs.powersync.com/architecture/powersync-protocol, /architecture/consistency,
  /architecture/powersync-service
- https://docs.powersync.com/maintenance-ops/self-hosting/deployment-architecture,
  /maintenance-ops/compacting-buckets
- https://docs.powersync.com/resources/performance-and-limits, /resources/feature-status
- https://docs.powersync.com/configuration/auth/overview
- https://hub.docker.com/r/journeyapps/powersync-service/tags; `powersync-service` `LICENSE`
  (FSL-1.1-ALv2)
- Unreachable: `api.github.com` and `github.com` HTML (proxy 403), so GitHub release pages were not
  read. Versions and dates come from Docker Hub and npm.
