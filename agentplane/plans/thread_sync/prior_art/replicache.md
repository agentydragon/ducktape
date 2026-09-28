# Prior art: Replicache (Rocicorp), from primary sources

Read 2026-09-23.

- **Status: maintenance mode.** From replicache.dev: "Replicache is now in maintenance mode. We have open-sourced the code
  and no longer charge for its use. … We will continue to support Replicache, but won't add new features. Existing
  users should migrate to Zero as they are able." Zero's client "uses replicache under the hood" (rocicorp/mono README).
- **Latest release: `replicache@15.3.0`, published 2025-07-02** (npm registry `time`). There has been no release since.
- **License is inconsistent.** On `main` of rocicorp/mono, the source is Apache-2.0 (root `LICENSE`, and
  `packages/replicache/package.json` says `"license": "Apache-2.0"`). The published 15.3.0 tarball still ships a
  `LICENSE` pointing at the 2022 Terms of Service, and its tag's `package.json` has `"license": "https://roci.dev/terms.html"`.
  `licenseKey`: "Replicache no longer uses a license key. This option is now ignored". No npm release yet carries
  the Apache license.
- **Size, measured from 15.3.0 `out/`:** the client bundle is 99.8 KB minified, 31.2 KB gzip. The server side is
  entirely ours ("BYOB"), and plain JSON-over-POST suits FastAPI.

## Protocol model

- **The client view** is one ordered KV map (string keys, JSON values) per **client group**. A client group is one
  browser profile times the Replicache `name`, so all tabs share it. It is persisted in IndexedDB, or held in memory
  with `kvStore: "mem"`.
- **Pull.** `POST pullURL {pullVersion, clientGroupID, cookie, profileID, schemaVersion}` returns
  `{cookie, lastMutationIDChanges, patch}`. `patch` is a list of `put` (the whole value), `del` and `clear`. It is
  applied in one transaction and revealed atomically. There is no append or partial-value op, and no pagination
  of a pull response.
- **The cookie** is opaque to the client and server-minted: any JSON that is orderable itself, or that carries an
  `order` field. It is the client's only stated position.
- **The server decides the content.** The pull handler computes the client view, and the protocol has no field
  for a client-declared query. The documented way to let the client steer it is to _sync the query as data_: a
  `/control/<user>/query` entity that a mutator changes and the pull handler reads. `pullURL` and `puller` are
  also runtime-settable in the public API, so the parameters can travel on the URL. That route is our design,
  not a documented pattern.
- **Diff strategies** decide how a pull computes the patch:
  - **Reset** re-sends everything every time.
  - **Global version** and **per-space version** send rows with `lastModifiedVersion > cookie`. The docs rate them
    "Partial sync: 👎🏼 Difficult".
  - **Row version** is the one that handles partial sync. The server stores a **CVR** per pull response: a map from
    each key the client holds to its version, stored under a random id that goes into the cookie. The next pull
    loads the prior CVR, reads `(id, version)` for the _current_ extent, and diffs the two. It `put`s keys that are
    new or whose version rose, and `del`s keys that dropped out.
- **Poke.** A contentless hint over any pubsub channel (SSE, WebSocket, Pusher) that makes the client call `pull()`.
  - Pulls are serialised. From the 15.3.0 source, not the docs: `maxConnections=1`, and the default `minDelayMs`
    is 30 ms, so bursts of pokes coalesce into one pull.
  - The experimental `rep.poke({baseCookie, pullResponse})` pushes a patch with no request, but "This method is
    under development and its semantics will change."
- **Push** sends mutations `{clientID, id, name, args}`. The server must commit their effects and the client's
  `lastMutationID` in the same transaction. The client rebases its pending mutations onto each pulled snapshot.
- **Versioning.** A `schemaVersion` the server rejects gets a `VersionNotSupported` response. The client then calls
  `onUpdateNeeded`, which by default runs `location.reload()`.

## Fit against the requirements

Evidence labels:

| Label    | URL                                                      |
| -------- | -------------------------------------------------------- |
| [HOME]   | https://replicache.dev/                                  |
| [HIW]    | https://doc.replicache.dev/concepts/how-it-works         |
| [OV]     | https://doc.replicache.dev/strategies/overview           |
| [RV]     | https://doc.replicache.dev/strategies/row-version        |
| [GV]     | https://doc.replicache.dev/strategies/global-version     |
| [PULL]   | https://doc.replicache.dev/reference/server-pull         |
| [PUSH]   | https://doc.replicache.dev/reference/server-push         |
| [CV]     | https://doc.replicache.dev/byob/client-view              |
| [POKE]   | https://doc.replicache.dev/byob/poke                     |
| [API]    | https://doc.replicache.dev/api/classes/Replicache        |
| [PERF]   | https://doc.replicache.dev/concepts/performance          |
| [BLOBS]  | https://doc.replicache.dev/howto/blobs                   |
| [LAUNCH] | https://doc.replicache.dev/howto/launch                  |
| [TRV]    | https://github.com/rocicorp/todo-row-versioning (README) |

Every row below assumes the **row-version** strategy. It is the only one the docs say handles partial sync.

| ID  | Verdict | Why                                                                                                                                                                                                                                         | Evidence                                                                                                                                                                                    |
| --- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C2  | +       | Pull and push are our own FastAPI endpoints, and there is no sync-engine server. Auth arrives as a header; a 401 makes the client call `getAuth`.                                                                                           | [HOME] "Replicache is client-side technology and works with most backend stacks." [PULL] "401 for auth error — Replicache will reauthenticate using getAuth"                                |
| P1  | +       | The extent is "last N rows of thread T", so the first pull is `clear` plus N `put`s. The IndexedDB cache makes a reopen instant.                                                                                                            | [RV] "This query can be any arbitrary function of the DB, including read authorization, paging, etc."                                                                                       |
| P2  | +       | Live updates are poke → pull. We build the poke channel: SSE fed by LISTEN/NOTIFY.                                                                                                                                                          | [HIW] "when data changes on the server, the server can send a poke to Replicache telling it to initiate a pull"                                                                             |
| P3  | +       | The CVR diff covers every key in the extent by version, not by position.                                                                                                                                                                    | [RV] "Fetch all entities from database that are new or changed between baseCVR and nextCVR"                                                                                                 |
| P4  | ~       | There is no paging primitive; scrolling back changes the extent. Growing it makes every pull read the `(id, version)` pairs of everything held. Moving it keeps work bounded but drops pages.                                               | [RV] query "might change from “all active threads" to "all active threads, or first 20 inactive threads ordered by modified-date”"                                                          |
| P5  | +       | Replicache never touches the DOM. A patch lands atomically and subscriptions re-fire with net results. Keys leave only when the extent drops them, and even a `clear` reset arrives as one atomic step.                                     | [CV] "they are revealed to your app (and thus to the user) all at once because they're applied within a single transaction"; the rest is `inferred`                                         |
| P6  | ~       | A failed pull retries with backoff, and resuming with the cookie gets only the diff. If the CVR has been evicted, the reply is a full reset: correct, but it re-transfers everything.                                                       | [API] "If the server endpoint fails pull will be continuously retried with an exponential backoff." [RV] "if the CVR is lost, the server can just send a reset patch"                       |
| P7  | +       | `schemaVersion` carries the projection epoch. A stale epoch gets `VersionNotSupported`, and the client reloads by default.                                                                                                                  | [API] onUpdateNeeded: "The default behavior is to reload the page (using location.reload())."                                                                                               |
| P8  | ~       | This can be expressed: make each body field its own key and put the field selection in the extent. There is no native projection parameter, so we build it.                                                                                 | [OV] "Sync any arbitrary subset of the database based on any logic you like."                                                                                                               |
| P9  | ~       | Replicache supports this natively (mutation ids, retry until confirmed, rebase). But our commands would have to go through `push` with `lastMutationID` bookkeeping, and client rows are never garbage-collected.                           | [PUSH] "Replicache will continue retrying a mutation until the server marks the mutation processed" [PULL] "Replicache does not currently support deleting client records from the server." |
| S1  | +       | One pull reads one DB transaction and applies atomically. Body keys named by reference cannot mismatch their metadata.                                                                                                                      | [CV] "applied within a single transaction"; dynamic-pull: "Read all data in a single transaction so it's consistent."                                                                       |
| S2  | +       | Scans follow key order, so an entity key built from a zero-padded `entity_index` scans in conversation order.                                                                                                                               | [LAUNCH] "The ordering of the keys when doing scan is a bytewise compare of UTF-8 encoded strings."                                                                                         |
| S3  | ~       | Each pull is an atomic snapshot, so no half-state can exist. The only "caught up" signals are the resolution of the `pull()` promise and `onSync(false)`; there is no per-unit watermark in the API. The cookie can carry one for our code. | [API] "onSync(false) is called … when Replicache transitions from at least one push or pull happening to none happening"                                                                    |
| S4  | +       | Row-version compares the version of _every_ key in the extent. Global and per-space version filter on `version > cookie`, the tail-shaped shortcut, and would miss rows that enter a moved extent.                                          | [RV] "Read all id/version pairs from the database that should be in the client view." [OV] global version "Partial sync: 👎🏼 Difficult."                                                     |
| E1  | +       | Opening costs one pull, plus one standing poke connection.                                                                                                                                                                                  | `inferred` from [PULL]                                                                                                                                                                      |
| E2  | +       | Bytes are bounded by the extent, which the server chooses.                                                                                                                                                                                  | `inferred` from [RV]                                                                                                                                                                        |
| E3  | ~       | Each poke costs one pull request, since pokes carry no data; bursts coalesce. Zero requests is possible only through the experimental `rep.poke()`.                                                                                         | [POKE] "A Replicache poke caries no data – it's only a hint telling the client to pull soon." [API] poke(): "under development and its semantics will change"                               |
| E4  | +       | The CVR diff sends only keys new to the extent.                                                                                                                                                                                             | [TRV] "The server will correctly send to the requesting client the difference from its last pull, even if the only thing that changed was the extent"                                       |
| E5  | ~       | Unchanged keys are never re-sent. A _changed_ key is re-sent whole, with no append op, so a body that grows while streaming is resent on every revision unless it is chunked into immutable keys. A lost CVR means a full resend.           | [PULL] patch ops: `put` / `del` / `clear` only. [PERF] "Max key-value size < 1MB"                                                                                                           |
| E6  | +       | A pull is triggered by a poke over a held channel (SSE here), not by a timer.                                                                                                                                                               | [POKE] "A Replicache poke caries no data – it's only a hint telling the client to pull soon."                                                                                               |
| O1  | ~       | Each pull response mints a new CVR, a map from every held key to its version, per client group. Nothing is shared between readers, and bounding it needs a TTL. Client-group and client rows are durable and never collected.               | [RV] "One CVR is generated for each pull response and stored in some ephemeral storage. The storage doesn’t need to be durable"                                                             |
| O2  | ~       | Pull and push are stateless once CVRs live in a shared store. Poke fan-out across replicas needs pubsub.                                                                                                                                    | [TRV] "for a distributed server (or serverless) you'll need to store these in something like Redis"                                                                                         |
| O3  | +       | The CVR is literally "what this client holds", and the diff is "why it got this row", both in our own code. A request-id header supports tracing.                                                                                           | [PULL] "This header is useful when looking at logs to get a sense of how a client got to its current state."                                                                                |
| O4  | ~       | Replicache adds no server engine. It does add a 31 KB gzip client framework in maintenance mode, and its core value (offline use, optimistic rebase, cross-tab sharing) is mostly not our problem.                                          | [HOME] "Replicache is now in maintenance mode … won't add new features."                                                                                                                    |
| D1  | +       | This is the row-version diff exactly. Holding 100–200 and pulling with extent 50–150 yields `put` for 50–99, `put` for the rows in 100–150 whose version rose, and `del` for 151–200. The caveat is per-tab windows (see below).            | [TRV] quote under E4; [RV] "op:del for every deleted entity"; [TRV] `cvr.ts` diff: `dels` = prev keys ∉ next                                                                                |
| D2  | ~       | Bodies in the client view are one mechanism with a parameter (the extent). The docs' out-of-band blob path is exactly the seam D2 warns against.                                                                                            | [BLOBS] "there is no guarantee that the blobs stays consistend with the state of Replicache"                                                                                                |
| D3  | ~       | The protocol can be adopted incrementally: a pull endpoint and cookie semantics with our own client. Adopting the library moves UI reads onto Replicache subscriptions, which lands whole.                                                  | `inferred`                                                                                                                                                                                  |

**Per-tab windows are the real friction.** The extent belongs to the client group (the profile and `name`), not to
a tab. [RV] says: "Changing the pull query in one tab changes it for other tabs that are sharing the same Replicache.
Without coordination, this could result in two tabs “fighting” over the current query." [PULL] says: "Make sure that
the client view is not a function of the client ID." A per-tab window therefore needs one of two things:

- a per-tab `name` (`${user}:${thread}:${tab}`), which gives a separate client group and IndexedDB database per
  tab and loses cross-tab sharing; or
- accepting one window per profile per `name`.

## What we would build vs get

**What we get from the library:**

- a persistent, ordered KV cache with atomic patch application and reactive `subscribe`
- pull scheduling: coalescing, backoff and 401 re-auth
- the cookie round-trip
- schema and epoch refusal plus reload (`onUpdateNeeded`)
- the optimistic-mutation and rebase machinery for P9, if we route commands through `push`
- a well-specified protocol (`PatchOperation`, cookie ordering, `lastMutationIDChanges`) to copy, even without the
  library

**What we would build either way:**

1. **A row-version pull endpoint** over the fold:
   - Read the extent from the query string: thread, `want` range, `content` fields.
   - Authorise the thread.
   - Read `(key, revision_cursor)` for the extent: entity rows plus the body keys the selection names.
   - Diff against the CVR and fetch the changed rows.
   - Return `put` / `del` and a cookie `{order, cvrID}`.
2. **A CVR store** in Postgres or Redis, with a TTL, and a strictly increasing `order` per client group.
   Alternatively, a stateless variant (below).
3. **A key design:**
   - `e/<thread>/<zero-padded entity_index>` for metadata and references
   - `b/<ref>/<field>` for bodies, which are immutable per reference, so a revision is a new key rather than a
     re-put
   - streaming bodies chunked into immutable keys to avoid E5's whole-value re-put
4. **A poke channel:** SSE per thread, fanned out across replicas with Postgres LISTEN/NOTIFY.
5. **Window movement:** set `rep.pullURL` to the new `want` and `content`, then `rep.pull()`. Plus the per-tab `name`
   decision above.

**The stateless variant is not documented; it follows from the cookie type.** The cookie may be any JSON with an
`order` field, so it can carry `{order, epoch, have, through}`. The server then computes the
<../option_moving_window.md> delta (want∖have whole, want∩have with `revision_cursor > through`, `del` for have∖want),
and no CVR is stored. That fixes O1 and O2.

It inherits Replicache's own warning against watermarks ([GV] § "Why Not Use Last-Modified?"): it is correct only if
`revision_cursor` becomes visible in commit order, per thread. A cursor assigned before commit by concurrent writers
can be skipped forever. **The same caveat applies to <../option_moving_window.md>'s `since`**, and a test should pin it.

**What Replicache does not give us:**

- a client-declared query or field projection (P8)
- paging (P4)
- append or partial-value patches (E5)
- a per-unit caught-up watermark (S3)
- zero-request streaming (E3), except through an experimental API
- shared per-conversation server state (O1)

**Net.** Replicache's _protocol_ (row-version CVR diff, or the stateless-cookie variant) is a documented, proven way
to get D1, E4, S4 and P7. Its _library_ mostly adds machinery we do not need, from a project in maintenance mode.

## Sources

- https://replicache.dev/ (status statement); https://doc.replicache.dev/sitemap.xml
- `https://doc.replicache.dev/concepts/{how-it-works,performance,consistency,offline}`
- `https://doc.replicache.dev/strategies/{overview,reset,global-version,per-space-version,row-version}`
- `https://doc.replicache.dev/byob/{client-view,dynamic-pull,poke}`, `…/reference/{server-pull,server-push}`
- `https://doc.replicache.dev/howto/{launch,blobs,source-access}`, `…/examples/{todo,repliear}`
- `https://doc.replicache.dev/api/classes/Replicache`, `…/api/interfaces/{ReplicacheOptions,RequestOptions}`,
  `…/api/type-aliases/{Cookie,Poke,Puller,UpdateNeededReason}`
- https://registry.npmjs.org/replicache — versions and dates; the 15.3.0 tarball was inspected for `LICENSE`, size
  and defaults
- `https://raw.githubusercontent.com/rocicorp/mono/main/{README.md,LICENSE,packages/replicache/package.json}`, the
  same `package.json` at tag `replicache/v15.3.0`, and `rocicorp/replicache/main/README.md`
- `https://raw.githubusercontent.com/rocicorp/todo-row-versioning/main/{README.md,server/src/pull.ts,server/src/cvr.ts,server/src/data.ts}`
- https://raw.githubusercontent.com/rocicorp/zero-docs/main/contents/docs/sync.mdx — "The predecessor to Zero"
- https://rocicorp.dev/terms — the 2022 Terms of Service the npm `LICENSE` points to
- https://github.com/rocicorp/replicache/issues/1033 — client-record GC, open (read through WebFetch)

**Unusable sources:**

- The Notion releases page (https://replicache.notion.site/Replicache-Releases-f86ffef7f72f46ca9b597d5081e05b88)
  rendered empty.
- https://github.com/rocicorp/replicache/releases returned 403 through the proxy. Its WebFetch summary gave
  implausible dates and was discarded, so release dates come from the npm registry only.
