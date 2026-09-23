# Zero (Rocicorp) against requirements.md

- **Status:** GA. "As of March 2026, Zero is generally available and fully-supported" (1.0.0 on npm
  2026-03-24). Latest stable is **1.9.0 (2026-08-14)**, with 1.10/1.11 canaries published daily.
  The roadmap is "largely responsive": bug fixes and performance, few new features.
- **License:** Apache-2.0 for the client and server ("no plans to ever change the licensing of the
  core product"). Hosted Cloud Zero is the business model.
- **Runtime:** the server is Node ≥22 (`rocicorp/zero:1.9.0` image). Clients must be TypeScript.
  Upstream must be Postgres ≥15 with `wal_level=logical`. Recommended for datasets under ~100 GB.
- Read on 2026-09-23 from zero.rocicorp.dev (`<page>.md`) and rocicorp/mono `main` @ `a86e4668`.
  No page 404'd. `gh api` was refused for rocicorp/mono in this session, so source came from
  raw.githubusercontent.com and a sparse clone.

## Protocol model

- **Transport.** The browser runs a `Zero` client with a local store (IndexedDB, or
  `kvStore:'mem'`). It opens **one WebSocket straight to a zero-cache view-syncer**, which is
  public on port 4848. Our app is not on that path: zero-cache calls it back over HTTP.
- **Unit of sync:** a named query plus its args (`defineQuery`). The server keeps state per
  **client group**, meaning all tabs of one browser profile, which share one CVR.
- **Who decides content.** For each new query, zero-cache POSTs `name` and `args` to our
  `ZERO_QUERY_URL`, batched as an array. It forwards cookies (`ZERO_QUERY_FORWARD_COOKIES`) or a
  bearer token. Our endpoint authenticates the caller, validates the args, and returns a ZQL
  **AST** or an app error. zero-cache runs that AST, never the client's own. The wire protocol
  still accepts a raw client AST, but with no legacy permissions deployed it "default[s] to not
  allowing any rows to be selected" (`zero-cache/src/auth/read-authorizer.ts`).
- **Data path.** One replication-manager owns a logical slot on a publication and keeps a SQLite
  replica. There can be N view-syncers, each holding its own copy of that replica, restored
  through Litestream from S3 in a multi-node setup. A view-syncer hydrates each query into an
  in-memory IVM pipeline **per client group**, then advances it on every replicated transaction.
- **CVR (client view record)** lives in Postgres (`ZERO_CVR_DB`). Table `cvr.rows` holds one
  record per synced row per client group: `rowVersion`, `patchVersion`, and
  `refCounts {queryHash: n}`.
  - The server sends "pokes" of row `put`/`del` patches, stamped with a CVR version. The client
    acknowledges by cookie.
  - Any patch whose `toVersion` is at or below the client's `baseVersion` is skipped
    (`client-handler.ts:279`). A row that is already present and unchanged keeps its old
    `patchVersion` (`cvr.ts:998`).
  - These two rules together are how Zero dedupes rows across overlapping queries and on
    reconnect.
- **Rows are sent whole.** ZQL has no column projection. The only way to narrow columns is the
  Postgres publication's column list, which applies to the whole app, not per reader.
- **Query lifetime.** A query stays active while it is in use. After that it keeps syncing for
  its `ttl` (default `5m`, maximum `10m`) and is then evicted. Rows that no remaining query
  references get `del` patches.
- **Status per query:** `unknown` (local data only), `complete` (server result received), or
  `error` (the transform failed).
- **Writes.** Mutators run optimistically on the client. zero-cache then POSTs them to
  `ZERO_MUTATE_URL`, which applies them to Postgres and records the mutation ID. Replication
  carries the result back, and the client rolls back its optimistic effects.

## Fit

`+` meets · `~` meets with work or a caveat · `−` fails · `?` unknown.

| ID  | V   | Reason                                                                                                                                                                                                                                                                        | Evidence                                                                                                                                                                                                           |
| --- | --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| C2  | ~   | **Authorization holds:** every synced row passes through our transform, which can force the thread predicate and clamp bounds. **"No direct browser path" fails:** the browser talks to zero-cache itself. Re-checking an open connection's auth is off by default.           | https://zero.rocicorp.dev/docs/self-host "View syncers must be publicly reachable by clients on port 4848." https://zero.rocicorp.dev/docs/zero-cache-config "Auth Revalidate Interval Seconds … default: `unset`" |
| P1  | +   | A tail query is one query: `orderBy('entity_index','desc').limit(N)` plus related bodies. On a cold start, the transform HTTP hop and the hydration come first.                                                                                                               | https://zero.rocicorp.dev/docs/self-host "Custom query transform latency … adding network + CPU before hydration starts."                                                                                          |
| P2  | +   | Changes arrive by logical replication, go through IVM, and are pushed as a poke.                                                                                                                                                                                              | https://zero.rocicorp.dev/docs/queries "It updates affected queries and sends row changes back to the client"                                                                                                      |
| P3  | +   | IVM reacts to any row that matches the query, wherever it sits.                                                                                                                                                                                                               | same quote; position-independence `inferred`                                                                                                                                                                       |
| P4  | +   | `start(row)` plus `limit` paging works in both directions. Rocicorp's `zero-virtual` library packages it.                                                                                                                                                                     | https://github.com/rocicorp/zero-virtual "Bidirectional infinite scrolling (load more items at top or bottom)"                                                                                                     |
| P5  | ~   | `zero-virtual` provides scroll anchoring and `useStickToBottom` "for chat / log UIs". It is 0.6.3, pre-1.0, React/Solid only. Without it, changing a window's args swaps that view's rows; the old rows stay in the store only for the TTL.                                   | https://github.com/rocicorp/zero-virtual "scroll anchoring keeps the viewport stable as rows load"                                                                                                                 |
| P6  | +   | The local store persists. On reconnect the client sends its cookie and the server replies with the CVR diff. Caveats: an inactive CVR is garbage-collected after 48 h; landing on another view-syncer means rehydration, which costs CPU but not bytes.                       | https://zero.rocicorp.dev/docs/zero-cache-config "CVRs … keep track of the data synced to clients in order to determine the diff to send on reconnect."                                                            |
| P7  | ~   | Pass the epoch as an arg and a `where`, and a stale query can never match new-epoch rows. The transform can also refuse with `error:'app'`, but it only runs when a query is added, on reconnect, or on the retransform interval — not because we deployed.                   | https://zero.rocicorp.dev/docs/queries "If the queries endpoint throws an application or parse error, zero-cache will report it to the client using the type and error fields"                                     |
| P8  | +   | Content selection works as `.related('chunks', q => q.where('field','IN',fields))`, with `fields` as a validated arg. It is live, and selecting nothing still returns entity rows. **Column projection is impossible**, but P8 selects rows by `field`, so it is not needed.  | https://zero.rocicorp.dev/docs/zql "There is no way to select a subset of columns" · "The `related` method accepts an optional second function which is itself a query."                                           |
| P9  | ~   | Zero's reconcile only covers commands written as Zero mutators. A Python push endpoint for them is undocumented. Keeping our own command path leaves P9 exactly as it is today.                                                                                               | https://zero.rocicorp.dev/docs/mutators "You can manually implement the mutate endpoint in any programming language. This will be documented in the future"                                                        |
| S1  | ~   | Pokes are consistent snapshots, so a relation keyed on the reference yields only the chunks that reference names. But our refs are JSONB, while ZQL correlates on column equality only and has no JSON filters. That means adding scalar `*_generation` columns.              | https://zero.rocicorp.dev/docs/queries "Zero always syncs a consistent partial replica" · `zero-protocol/src/ast.ts` "Only equality correlation are supported for now."                                            |
| S2  | +   | The client's materialized view keeps the `orderBy` order, whatever order rows arrive in.                                                                                                                                                                                      | https://zero.rocicorp.dev/docs/zql "You can sort query results by adding an `orderBy` clause"; arrival-order independence `inferred`                                                                               |
| S3  | ~   | Each query reports `complete` or `unknown`. `complete` only means the server result has arrived once; there is no "caught up to head" watermark.                                                                                                                              | https://zero.rocicorp.dev/docs/queries "The `complete` value is currently only returned when Zero has received the server result."                                                                                 |
| S4  | +   | IVM covers every row in the result, with no recency window. If advancing costs more than rehydrating, zero-cache rehydrates. Caveat: 1.9 fixed "Rebuilt queries now deliver changed rows instead of occasionally leaving clients with stale results".                         | https://zero.rocicorp.dev/docs/self-host "View syncer's IVM is 'hydrate once, then incrementally push diffs'" · https://zero.rocicorp.dev/docs/release-notes/1.9                                                   |
| E1  | +   | Opening a thread is one WebSocket carrying query patches, plus one batched transform POST from zero-cache to us.                                                                                                                                                              | https://zero.rocicorp.dev/docs/queries "The endpoint receives a `POST` request with a JSON body of the form: `{id,name,args}[]`"                                                                                   |
| E2  | +   | Bytes are bounded by `limit` and the filtered `related` bodies. Entity rows are sent whole, JSONB `state` included.                                                                                                                                                           | `inferred`                                                                                                                                                                                                         |
| E3  | +   | Updates are pushed as pokes over the open socket: zero requests per arriving item.                                                                                                                                                                                            | https://zero.rocicorp.dev/docs/queries "sends row changes back to the client"                                                                                                                                      |
| E4  | +   | Only the new page's rows are sent. The overlap is deduped by the CVR (`patchVersion` is kept, and the patch is skipped at or below `baseVersion`).                                                                                                                            | `zero-cache/src/services/view-syncer/cvr.ts:998` "existing.patchVersion // existing row is unchanged"; `client-handler.ts:279`                                                                                     |
| E5  | ~   | Nothing held in the CVR is re-sent. Data is re-sent after TTL eviction, CVR garbage collection, or a fresh client group. **Any edit re-sends the entire row**; there are no column deltas.                                                                                    | https://zero.rocicorp.dev/docs/queries "Zero does not sync duplicate rows: Zero syncs the _union_ of all active queries' results."                                                                                 |
| E6  | +   | One WebSocket; row changes are pushed as pokes.                                                                                                                                                                                                                               | https://zero.rocicorp.dev/docs/queries "sends row changes back to the client"                                                                                                                                      |
| O1  | ~   | Server state is bounded (TTL at most 10 m, CVR garbage collection) but **not shared**: a CVR row record per synced row per client group in Postgres, plus pipelines per client group. Only the replica is shared.                                                             | https://zero.rocicorp.dev/docs/self-host "each client group has its own pipelines" · `view-syncer/schema/cvr.ts` `"refCounts" JSONB -- {[queryHash: string]: number}`                                              |
| O2  | ~   | View-syncers scale horizontally; there is exactly one replication-manager. Sticky sessions are needed. Multi-node needs Litestream backups to S3. Our query endpoint is stateless.                                                                                            | https://zero.rocicorp.dev/docs/self-host "Number deployed \| 1 \| N (horizontal scale)" · "important to try to keep clients connected to the same instance"                                                        |
| O3  | ~   | A browser inspector shows each query's server ZQL, row counts and synced rows, and there are OTEL metrics. "Why this row" means querying the internal `cvr.rows.refCounts`, not our logs.                                                                                     | https://zero.rocicorp.dev/docs/debug/inspector "`serverZQL` \| The server-side ZQL that your `get-queries` endpoint returned for this query."                                                                      |
| O4  | −   | A new Node engine with two process roles, a SQLite replica on fast disk per view-syncer, Litestream plus S3, CVR/CDC schemas, a second replication slot, event triggers, a TypeScript schema, and duplicated query definitions.                                               | https://zero.rocicorp.dev/docs/self-host "you will need to deploy zero-cache, a Postgres database, your frontend, and your API server"                                                                             |
| D1  | +   | Moving 100–200 to 50–150 sends only 50–99 plus changed rows in 100–150. Rows 151–200 are deleted when the old query leaves the CVR (after the TTL). **Edge:** `ttl:'none'` with the old query removed before the new one is added would drop the overlap and then re-send it. | `cvr.ts:998`, `client-handler.ts:279` (dedupe); edge `inferred`                                                                                                                                                    |
| D2  | +   | Streaming and on-demand bodies are one mechanism: another query, or a different `fields` arg. Both are live and both get the same dedupe.                                                                                                                                     | `inferred` from the P8 and E4 mechanics                                                                                                                                                                            |
| D3  | ~   | Zero can run beside Electric on the same Postgres with its own app ID, slot and publication, one view at a time. But the first step is the whole engine plus the TypeScript schema plus the transform endpoint.                                                               | https://zero.rocicorp.dev/docs/zero-cache-config "Multiple zero-cache apps can run on a single upstream database, each of which is isolated from the others"                                                       |

## What we would build vs get

**We get, by construction:**

- D1, E4, P8 and D2. A reader's window is a query, and moving it pays only for the difference.
- S4. Any edit inside a held window is delivered, and there is no tail assumption to get wrong.
- P6. Reconnect resumes from the CVR.
- Consistent multi-table pokes.
- A tested chat scroller (`zero-virtual`) that is optional and pre-1.0.

**Deployment.** This replaces Electric in the end state.

- zero-cache as a replication-manager (1 replica, with a PVC or emptyDir) plus view-syncers
  (N ≥ 2, ClientIP stickiness, ~10 min startup grace, fast-IOPS volume).
- A Litestream bucket (S3-compatible); multi-node requires one.
- `ZERO_CVR_DB` and `ZERO_CHANGE_DB` behind a pooler; `ZERO_UPSTREAM_DB` must be a direct
  connection.
- An explicit publication limited to the fold tables and columns, via `ZERO_APP_PUBLICATIONS`.
- Event-trigger rights for zero-cache, i.e. a superuser role. Without it, "any schema change
  triggers a full reset".
- Settings: `ZERO_ENABLE_CRUD_MUTATIONS=false`, `ZERO_QUERY_API_KEY`,
  `ZERO_AUTH_REVALIDATE_INTERVAL_SECONDS`, and telemetry off.
- `wal_level=logical` and `REPLICA IDENTITY FULL` already exist for Electric (`inferred` from
  migration `0005`).

**Python backend.** This is the part the docs under-specify.

- Add `POST /api/zero/query` in FastAPI. It must:
  1. authenticate the forwarded cookie or bearer token;
  2. check the reader's entitlement to the thread;
  3. clamp ranges;
  4. emit **Zero AST JSON** using server-side table and column names;
  5. return `{kind:'QueryResponse', queries, userID}`.
- The docs say other languages are possible. But the documented body leaves out the
  `['transform', […]]` envelope and lists error kinds `app|zero|http`, while the source has
  `app|parse` (`zero-protocol/src/custom-queries.ts`, `zero-server/src/queries/process-queries.ts`).
  So: follow the source, pin the Zero version, and add a contract test against a real zero-cache.
- Commands stay on our existing path. Porting them to mutators would need an undocumented Python
  push protocol.

**Schema.**

- A hand-written `schema.ts` that mirrors our Alembic tables. Zero's generators cover only
  Drizzle and Prisma.
- Add scalar correlation columns so an entity can relate to _its_ chunk generation (for example
  `text_generation`), because refs are JSONB and ZQL has neither JSON filters nor
  non-equality joins.
- Add `entity_index` as a column.
- Our column types (text, bigint, bool, jsonb) and compound primary keys are all supported.

**Client.**

- `@rocicorp/zero` and its React bindings.
- A TypeScript copy of each query definition. The client runs it locally for instant results;
  the server version may differ, so it is duplicated logic.
- Cookie auth needs zero-cache on a subdomain, a root-`Domain` cookie, and `SameSite=Lax`. The
  docs warn against `SameSite=None` because of cross-site WebSocket hijacking.
- Bundle size is not documented. The npm package unpacks to 8.6 MB including server code, so the
  real client size is unmeasured.

**C3 and C2 caveats.**

- A staging reset now also means wiping the replica. From llms.txt: "Resetting the database …
  requires also deleting the SQLite replica and restarting zero-cache".
- Meeting the letter of C2 means putting a WebSocket reverse proxy in our app in front of
  zero-cache. That is undocumented and adds nothing to authorization, because every read already
  goes through our transform.

## Sources

- `https://zero.rocicorp.dev/docs/{introduction,status,release-notes,release-notes/1.9,release-notes/1.5,release-notes/1.0,open-source,when-to-use,sync,cloud-zero}`
- `https://zero.rocicorp.dev/docs/{queries,auth,zql,mutators,schema,connection,self-host,zero-cache-config,postgres-support,connecting-to-postgres,debug/inspector}`
- https://zero.rocicorp.dev/llms.txt
- https://registry.npmjs.org/@rocicorp%2Fzero and https://registry.npmjs.org/@rocicorp%2Fzero-virtual (versions, dates, license)
- https://github.com/rocicorp/zero-virtual (README)
- rocicorp/mono @ `a86e4668`, fetched as `https://raw.githubusercontent.com/rocicorp/mono/main/packages/…`:
  - `zero-cache/src/services/view-syncer/{cvr.ts,client-handler.ts,view-syncer.ts,schema/cvr.ts}`
  - `zero-cache/src/auth/read-authorizer.ts`
  - `zero-protocol/src/{ast.ts,custom-queries.ts,queries-patch.ts}`
  - `zero-server/src/queries/process-queries.ts`
