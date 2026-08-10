# "Past tool calls" is ridiculously slow and freezes the tab (2026-08-10)

## Symptom

The console's full-page history view (`/_console/tool-calls`) took a long time to load, and froze
the browser once it finally rendered.

## Where the time actually went

Measured rather than guessed, because the report did not say whether the backend or the frontend
was at fault. It was neither the query nor the endpoint: it was **how much the endpoint was asked
for, how it travelled, and what the page built out of it**.

### Backend: the query is not the problem

A throwaway `py_test` seeded 50 000 ledger rows (realistic payloads: ~5 KB results with a heavy
tail to 25 KB, half of them auto-approved) and timed `GET /api/tool-calls?limit=…&newest_first=
true&auto_approved=false` through `TestClient`:

| limit | endpoint | response    |
| ----- | -------- | ----------- |
| 25    | 14 ms    | 89 KB       |
| 100   | 12 ms    | 356 KB      |
| 500   | 24 ms    | **1.78 MB** |

`EXPLAIN ANALYZE` of the scoped list query: 3.1 ms, an `Index Scan Backward using
idx_mcp_tool_calls_created_at` feeding nested loops on the principal/binding/agent joins. No
sequential scan, no sort, and it does not degrade with ledger size.

So the endpoint is fine and the index is right. What it hands back is not: at the view's old
`limit=500` a page was ~1.8–2.6 MB of JSON, and the view refetched the whole thing on **every**
live WS event plus a 30 s heartbeat.

### The wire: nothing was compressed

`curl -I -H 'Accept-Encoding: gzip' https://haku.allegedly.works/_console/assets/main-*.js` on the
live console returned no `content-encoding`, `content-length: 1795107`. The static image's nginx
never turned gzip on, so both the SPA bundle (1.79 MB, ~497 KB gzipped) and every API response
went out raw — the megabytes above at full size.

### Frontend: 500 CodeMirror editors

A temporary `history-perf` scene in the existing screenshot harness (real headless Chromium on
RBE, `PerformanceObserver` on `longtask`, mock fetch so **no** network cost is included) rendered
the view at the old 500-row page:

|                            | before                           | after         |
| -------------------------- | -------------------------------- | ------------- |
| blocked main thread        | **15 072 ms** over 14 long tasks | 578 ms over 3 |
| longest single task        | **5 078 ms**                     | 305 ms        |
| DOM nodes                  | 26 957                           | 1 421         |
| CodeMirror editors mounted | 201                              | 2             |

Each row renders its arguments through `JsonPreview` → `CodeBlock`, i.e. one `EditorView`: DOM, a
lezer parse, and for a compact block a per-frame `requestAnimationFrame` poll until the fold pass
can run. Hundreds of those built in one commit is the freeze, and a single 5 s task is exactly the
"browser froze" report — with an instant mock fetch, so the real thing had the 2 MB download in
front of it.

## Fixes

1. **Keyset paging** (`cursor`/`next_cursor` on `GET /api/tool-calls`, `ToolCallPageCursor`), and
   the view reads 25 rows with "Load older calls". Keyset rather than offset because the view
   refetches its first page on live events and calls are submitted into the top of the order
   between pages.
2. **Live events refetch only the first page** and merge it over what is loaded, with at most one
   refresh in flight so a burst of events collapses into one catch-up.
3. **`CodeBlock` builds its editor only when it nears the viewport** (`IntersectionObserver`,
   600 px margin), reserving the height in a placeholder until then. This also covers a collapsed
   `Raw arguments`/`Raw result` disclosure, whose contents are in the DOM but have no box.
4. **gzip in the static image's nginx**, for what it serves and what it proxies
   (`gzip_proxied any`), excluding `text/event-stream` so `/mcp` still flushes per event.

## What was thrown away

The 50 000-row `py_test` and the `history-perf` screenshot scene were scaffolding for these
numbers, not tests — nothing asserted a threshold — so they are not in the tree. Re-create them
the same way if this regresses: a scene whose mock ledger is large, and a `longtask`
`PerformanceObserver` read after the editor count stops growing.
