# Prior art, scored

Read on 2026-09-23 from primary docs, and from source where the docs were silent. Each file below
scores one system against every ID in <../../../docs/thread_sync_requirements.md>, with a URL and a verbatim quote per
verdict, or `inferred` where nothing states it.

- <electric_powersync.md> — ElectricSQL as it is now (the engine we run), and PowerSync.
- <zero.md> — Zero (Rocicorp).
- <replicache.md> — Replicache (Rocicorp), scored as a protocol.
- <others.md> — Convex, InstantDB, LiveStore, Triplit and Phoenix LiveView, decisive verdicts only.

## Summary

`+` meets it, `~` meets it with work or a caveat, `−` fails it. **C1** is whether the system can
serve our Postgres fold rather than its own store.

| System                         | C1  | C2  | P8  | S4  | D1        | E6  | O1  | O2  | O4  | State (2026-09)                                         |
| ------------------------------ | --- | --- | --- | --- | --------- | --- | --- | --- | --- | ------------------------------------------------------- |
| Electric, one shape per thread | +   | +   | +   | +   | +         | +   | +   | ~   | ~   | 1.8.1, Apache-2.0; the company joined Databricks        |
| PowerSync                      | +   | −   | +   | +   | ~ (pages) | +   | +   | +   | −   | service 1.26.1, FSL (Apache-2.0 after two years)        |
| Zero                           | +   | ~   | +   | +   | +         | +   | ~   | ~   | −   | GA since 1.0 (2026-03), 1.9.0, Apache-2.0               |
| Replicache, row-version pull   | +   | +   | ~   | +   | +         | +   | ~   | ~   | ~   | maintenance mode, last release 2025-07                  |
| Convex, InstantDB, LiveStore   | −   |     |     |     |           |     |     |     |     | each owns its store; Triplit also, and is dormant       |
| Phoenix LiveView streams       | +   | +   |     | +   | ~         | +   | +   | ~   |     | a pattern for <../option_app_push.md>, not a dependency |

**D4** (streaming costs the delta) was added after the survey. Electric, Zero and PowerSync sync rows,
so our insert-only chunk rows give it to them; Replicache needs bodies split into immutable chunk
keys, since it re-sends a changed value whole; Convex fails it, re-sending the whole query result.

**Nothing here is adopted whole.** The four engines that can read our Postgres split on O4 and C2:

- **Electric** is the only one already running, and reaches **D1** inside one fixed shape per
  thread, which is what is deployed (<../../../docs/thread_view_sync.md>). Its remaining costs are a live log that carries every change in the
  thread, one active instance per replication slot, and a `409` that makes the stock client drop a
  shape's rows (the store keeps them on screen through the refetch).
- **Zero** gets D1, E4, P8 and D2 by construction, with our app authorizing every query through a
  callback. It costs a new Node engine with two process roles, a SQLite replica per view-syncer, S3
  backups, a second replication slot and superuser event triggers, plus query definitions in both
  TypeScript and Python. The browser holds a WebSocket straight to it.
- **PowerSync** puts the browser on the engine with a bearer token (C2 `−`), and its subscription
  parameters are equality-only, so a window is a stored page number and never an index range.
- **Replicache**'s _library_ is in maintenance mode and mostly solves offline use and optimistic
  rebase. Its _protocol_ is the valuable part (below).

## What to take from it

1. **Electric has no Postgres- or object-storage shape backend.** Storage is memory or local files,
   with one active instance per replication slot; scaling out means a CDN, or separate instances
   with their own slots behind sticky sessions. Shared network storage is supported in an exclusive
   mode.
2. **The moving window is Replicache's row-version pull.** Replicache stores, per client, the
   version of every row it sent and diffs the next window against it; a cookie carrying
   `{have, through, epoch}` instead gives the same diff with no server state, which is
   <../option_moving_window.md>. Replicache's docs warn why a watermark can be wrong: it is correct
   only if a thread's `revision_cursor` becomes visible in commit order. A test has to pin that
   before `since` is trusted.
3. **Ideas worth copying**, from systems that are not adoptable:
   - pages pinned by key range, so edits grow or shrink a page but never shift it — which
     `entity_index` gives us as long as it never renumbers (Convex);
   - pages capped by bytes, split on the server's advice (Convex);
   - one watermark across all of a reader's watches, so "caught up" is one number (Convex);
   - the client stating what it holds on rejoin (Convex's agent streaming, Phoenix Channels'
     `last_seen_id`) — the same `have`/`since` shape;
   - for server push: the server keeps only the window bounds, the client prunes the DOM without
     telling it, older pages are inserted above an anchored first child, and edits are sent
     update-only so they reach held rows without the server tracking them (LiveView streams).
