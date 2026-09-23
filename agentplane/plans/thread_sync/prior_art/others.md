# Prior art, part 2: Convex, InstantDB, LiveStore, Triplit, Phoenix LiveView

Read on 2026-09-23 from primary docs. Where the docs said nothing, the claim comes from the
project's own source and is marked **source**. Verdicts use the IDs in <../../../docs/thread_sync_requirements.md>.
**inferred** means no page states the claim directly.

**Bottom line.** Convex, InstantDB, LiveStore and Triplit each own the store clients read, so
under **C1** adopting any means a second copy of the fold. **None can be adopted.** LiveView is a
pattern for <../option_app_push.md>, not a dependency. Worth copying:

1. **Pages pinned by key range** (Convex journal): edits grow or shrink a page, never move it.
2. **Byte-capped pages, split on server advice** (Convex `SplitRecommended`).
3. **One watermark across all of a reader's watches** (Convex `Transition`), so **S3** is one number.
4. **The client states what it has** (Convex agent `streamArgs.cursors`, Phoenix Channels
   `last_seen_id`): two unrelated systems reached the `have`/`since` shape of
   <../option_moving_window.md>.
5. **Server keeps only window bounds; edits go out update-only** (LiveView `limit`, `update_only`).

## Convex

- `convex` 1.46.0 (npm, 2026-09-16); `@convex-dev/agent` 0.7.3 (2026-09-14). Actively maintained.
- The backend is "FSL Apache 2.0", which is "automatically converted to full Apache-2.0 two years
  after its creation". The client is Apache-2.0.
- Self-hosting runs one backend binary. Storage is SQLite by default, or Postgres 17 / MySQL 8.

**Model.**

- Queries are TypeScript functions over Convex tables. Per WebSocket session the sync worker holds
  the query set and each query's _read set_ (index ranges scanned). A commit overlapping a read
  set reruns the function and pushes the **whole new return value** as `QueryUpdated{value,
journal}`, no diff (**source** `protocol.ts`). One `Transition{startVersion, endVersion,
modifications}` moves all of a client's queries to the same snapshot.
- Paging uses `.paginate({numItems, cursor, endCursor?, maximumRowsRead?, maximumBytesRead?})`,
  which returns `{page, continueCursor, isDone, splitCursor?, pageStatus?}`.
  `usePaginatedQuery` keeps one subscription per page, and `loadMore` adds the next one.
- **Stable boundaries.** The first execution records the page's end cursor in a _journal_ the
  client sends back on every rerun, so the page stays the key range `(cursor, endCursor]`: it
  grows or shrinks, and rows never fall between pages or appear on two.
- **Splitting.** A page that reads too much returns `SplitRecommended` or `SplitRequired` with a
  `splitCursor`. The client subscribes to the two halves, then drops the original.
- `usePaginatedQuery` only grows, forward. `convex-helpers` `getPage` returns every row's index
  key, so an app can page both ways, jump, drop invisible pages and split.
- Convex's own AI-agent component does not stream tokens through paginated reruns: it writes
  deltas to a table and the client passes `streamArgs: {kind: "deltas", cursors: [{streamId,
cursor}]}`.

**Verdicts that decide it.**

- **C1 −**: Convex stores data only in its own `documents(id, ts, table_id, json_value, …)` table,
  in a database of its own, and functions can read only Convex tables. Evidence:
  `postgres_or_mysql.md`, "Create a database called `convex_self_hosted`"; **source**
  `crates/postgres/src/sql.rs`, `CREATE TABLE … documents`.
- **P5 +**: boundaries are pinned by key, not by count. `fully-reactive-pagination`: "Whenever the
  query is recomputed, Convex ignores the numItems parameter and instead returns all items until
  the end cursor."
- **E3/E5 −**: any change inside a page re-sends the whole page. `how-convex-works`: "the sync
  worker reruns the function and pushes its updated return value to the client." The agent
  component's delta cursors look like the workaround for this (**inferred**).
- **D1 ~**: `loadMore` adds a page that doesn't overlap the others, so nothing is re-sent. A split
  does re-send the split page, as two new subscriptions (**source** `use_paginated_query.ts`).
- **P6 −**: on reconnect, `restart()` re-adds every query with its journal. The server answers each
  one with a full `value`, so the client downloads everything it held again (**source**
  `local_state.ts`).
- **O1 −**: each session keeps its own subscriptions. "we keep track of its read set in the
  client's WebSocket session within the sync worker." Readers share the result cache, but not
  their subscriptions.
- **S3 +**: `realtime`: "Every client subscription gets updated simultaneously to the same
  snapshot of the database."

**Verdict: don't adopt (C1, O4); copy ideas.** Copy: pin a page's end on first read
(`entity_index` ranges get this free if the index never renumbers); byte-capped pages the server
can advise splitting; one snapshot watermark across window and tail watches. Don't copy: full
re-push on every change, refetch-everything on reconnect.

## InstantDB

- `@instantdb/core`/`react` 1.0.67 (2026-08-31), Apache-2.0, maintained. Self-hosting documented
  (VPS from ~$30/mo, AWS from ~$600/mo): Clojure server on Postgres.

**Model.**

- Clients declare InstaQL (GraphQL-shaped) queries. The server keeps everything in one
  `triples(app_id, entity_id, attr_id, value, …)` table, tails the WAL, maps entries to _topics_
  and refreshes matching queries. The client keeps a triple store persisted to IndexedDB.
- Paging is `limit`/`offset`, or connection cursors (`first`+`after`, `last`+`before`, with
  `pageInfo.{startCursor, endCursor}`). Both work on top-level namespaces only.
- `useInfiniteQuery` pages in one direction only (`loadNextPage`).
- `$: {fields: [...]}` selects fields, including on nested relations.

**Verdicts that decide it.**

- **C1 −**: README: "we store all user data as triples in one big Postgres database." To use it,
  the fold would have to be written into Instant's schema, making a second copy.
- **P8 +**: `instaql`: "If you prefer to select the specific fields that you want your query to
  return, use the `fields` param".
- **D1 −**: `infinite-queries`: "Changing any part of the query will result in a full reset of
  all data, returning back to a state with only one page loaded."
- **P4 ~**: plain queries page in both directions ("use the `startCursor` in the `before` field …
  and ask for the `last` items"). The live infinite hook only pages forward.
- **O2/O4 −**: `self-hosting`: "Every server must share the same configuration, discover the other
  servers, and communicate over the Hazelcast and gRPC ports."

**Verdict: ignore** (C1, O4). Borrow only vocabulary: `fields` as a query parameter, and
`first/after/last/before` + `pageInfo` if we expose cursor paging.

## LiveStore

- `@livestore/livestore` 0.4.0 (2026-06-02), 0.5.0-dev in progress; Apache-2.0. Self-described
  **beta**, "not yet ready for all production scenarios".

**Model.** LiveStore is event-sourced. Clients push and pull an _eventlog_ through a central sync
backend, git-style with rebase. The backend sets the total order. Each client folds the log into
an in-memory SQLite database.

Each store syncs in full: "All the client app data should fit into a in-memory SQLite database."
The docs' answer for more data is many stores (`storeId` per project or document). The syncing
reference and the when-to-use page describe no partial sync within a store.

- **C1/C4 −**: `when-livestore` lists this under "Reasons when not to use LiveStore": "You have an
  existing database which is the source of truth of your data. (Better use Zero or ElectricSQL
  for this.)". The FAQ, asked whether an existing database can be used: "Not currently."
- **P1/E2 −**: with one store per thread, opening a thread ships and folds its whole log in the
  browser, which is the opposite of showing the tail first (**inferred** from full sync).

**Verdict: ignore.** It moves the fold into the client, and C1 keeps it in Postgres.

## Triplit

- `@triplit/client` 1.0.50 / `@triplit/server` 1.1.8, both 2025-07-31; AGPL-3.0; last commit on
  `main` 2025-09-11. **Dormant.** Co-founder joined Supabase 2025-10-08 to work on integrations
  with _other_ sync engines; the post promises more open-sourcing, not development.
- `triplit.dev` returned 503 / connection reset on 2026-09-23 and was not read.

**Model.** A "full stack database" with its own server storage (SQLite, LevelDB, memory).
Clients declare queries (`Where`, `Order`, `Limit`, `Select`) synced over WebSocket; replication
is partial and delta-based (FAQ: servers "only send data to the clients that are listening for it
and that have not yet received it"). Paging: `subscribeWithPagination` (`nextPage`/`prevPage`),
`subscribeWithExpand` (a growing window).

- **C1 −**: own storage, no mode for reading existing Postgres tables (README). **Maintenance −.**

**Verdict: ignore.** The phrase "not yet received" is `have` tracked on the server side. That
needs per-client memory on the server (O1). Our `have` keeps that memory in the client.

## Phoenix LiveView streams / Channels

- `phoenix_live_view` 1.2.12 (2026-09-16), `phoenix` 1.8.14 (2026-09-14); MIT; very active.
  A reference for <../option_app_push.md>, not a dependency.

**Model.**

- One server process per client holds assigns and sends rendered diffs. **Streams live in the
  DOM, not the process**: "Stream items are temporary and freed from socket state immediately
  after the render/1 function is invoked." API: `stream(…, at:, limit:, reset:)`,
  `stream_insert(…, at:, limit:, update_only:)`, `stream_delete`; re-inserting a DOM id updates
  it in place.
- **Pruning is client-only**: "A positive limit will prune items from the end of the container,
  while a negative limit will prune items from the beginning". The server is never told.
- **Two-way paging** (bindings guide): the process keeps only `page`/`per_page`.
  `phx-viewport-top` sends `prev-page` and the server streams that page with
  `at: 0, limit: per_page*3`; `phx-viewport-bottom` uses `at: -1` and a negative limit. The DOM
  holds about 3 pages. The example pages by `offset`.
- **Scroll position**: when the top event fires, the `InfiniteScroll` hook remembers the first
  child and, after the patch, calls `firstChild.scrollIntoView({block: "start"})` if it left the
  viewport (**source** `hooks.ts`). `pt-[calc(200vh)]`/`pb-[calc(200vh)]` padding leaves room to
  scroll. Dragging the scrollbar past the top sends `_overran: true`, and the example resets to
  page 1.
- **Edits**: with `update_only: true`, "If the item does not exist on the client, it will not be
  inserted".
- **Reconnect**: the client rejoins and the server runs `mount/3` again in a new process. The join
  patch then does "clear stream items from the dead render if they are not inserted again"
  (**source** `dom_patch.ts`).
- **Channels**: "at-most-once … If the client is offline and misses the message, Phoenix won't
  resend it". The guide suggests having the client pass `last_seen_id` when it joins.

**Verdicts that decide it.**

- **E4 +**: scrolling back fetches only the new page, inserted at 0. **D1 ~**: a page `limit`
  pruned is fetched again on return — requirements § D1's "honest edge", here by design.
- **P5 ~**: paging never re-renders items the client holds, and the first child stays anchored.
  But an overrun resets to page 1, and the anchor aligns an element to the top rather than
  keeping the exact pixel offset.
- **P6 −**: reconnect runs mount again. Items the new mount does not re-send are removed, and
  items it does re-send are downloaded again. Deployments guide: "your LiveView may still have
  state that will be lost in this transition."
- **S4 +**: with `update_only`, one change feed per thread can update whatever the client holds,
  and the server never tracks a held set. The cost is that every reader receives every change and
  discards the ones it doesn't hold (the pattern is **inferred**; the flag is documented).
- **O1 +**: per-client state is `page` plus the render tree; items are not kept. Readers share a
  PubSub topic, not processes. **O2 ~**: pinned to one node; recovery is reconnect + remount.

**Verdict: copy ideas into <../option_app_push.md>.** Copy: the server keeps only window bounds; the
client prunes with `limit` without telling it; prepend at 0 and anchor on the old first child;
`update_only` for edits; `last_seen_id` on rejoin (our `since`). Don't copy: remount on reconnect
(fails P6); offset paging, which shifts under inserts where `entity_index` ranges do not.

## Sources

- **Convex**: docs.convex.dev `database/pagination`, `api/interfaces/server.Pagination{Options,Result}`,
  `realtime`, `self-hosting`, `agents/streaming`; stack.convex.dev `fully-reactive-pagination`,
  `pagination`, `how-convex-works`; convex-backend `self-hosted/advanced/postgres_or_mysql.md`,
  `crates/postgres/src/sql.rs`; convex-js `src/browser/sync/{protocol,local_state}.ts`,
  `src/react/use_paginated_query.ts`; get-convex/agent `src/validators.ts`.
- **InstantDB**: instantdb.com/docs `instaql`, `infinite-queries`, `self-hosting`;
  `essays/architecture`; github.com/instantdb/instant README.
- **LiveStore**: docs.livestore.dev `evaluation/when-livestore`, `misc/faq`, `reference/syncing`,
  `overview/how-livestore-works`, `misc/state-of-the-project`.
- **Triplit**: github.com/aspen-cloud/triplit (README; `packages/docs/.../faq.mdx`,
  `subscribe-with-pagination.mdx`, `subscribe-with-expand.mdx`, `query/select.mdx`; commit
  history); supabase.com/blog/triplit-joins-supabase. `www.triplit.dev` was unreachable.
- **Phoenix**: hexdocs.pm `phoenix_live_view/Phoenix.LiveView.html` (`stream/4`,
  `stream_insert/4`), `phoenix_live_view/bindings.html` (scroll events), `phoenix_live_view/
deployments.html`, `phoenix/channels.html` (Resending Server Messages); phoenix_live_view
  `assets/js/phoenix_live_view/{hooks,dom_patch,view}.ts`.
- Versions: registry.npmjs.org, hex.pm.
