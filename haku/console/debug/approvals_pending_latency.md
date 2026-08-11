# `GET /api/approvals/pending` — where the ~2s goes

Investigation 2026-08-11. Report: the endpoint takes ~2s in the browser.

Status: **root cause not confirmed.** Two candidates were measured and eliminated; the
remaining leading hypothesis needs prod access to settle. Recorded here so the next pass
starts from the measurements rather than repeating them.

## Eliminated: the ledger query

The obvious suspect was the missing index. `mcp_tool_calls` carries exactly one index
(`idx_mcp_tool_calls_created_at`, migration `0010`) and none on `status`, so the pending read
(`WHERE status = 'pending_approval' ORDER BY created_at, tool_call_id LIMIT 100`) has no
access path for its filter — with few pending rows it must examine the whole table.

Measured against a real Postgres 18 on RBE: 60k tool calls, 3 of them pending, joined through
`mcp_tool_call_principals` → `credential_bindings` → `agents` → `agent_name_reservations`
exactly as `_record_projection_stmt` builds it.

|                                                                     | Postgres `Execution Time` | end-to-end from `list_tool_calls` |
| ------------------------------------------------------------------- | ------------------------- | --------------------------------- |
| as deployed (seq scan, 59 997 rows discarded)                       | 4.9 ms                    | 9.6–20.6 ms                       |
| with `(created_at, tool_call_id) WHERE status = 'pending_approval'` | 0.1 ms                    | 12.5–17.4 ms                      |

The scan is real (`Rows Removed by Filter: 59997`, 12 000 shared buffers) but costs
milliseconds, and the partial index is invisible end-to-end — Python-side row construction
dominates. **A missing index is not a 2-second effect at this table size.** Worth adding on its
own merits before the ledger grows an order of magnitude, but it is not this bug.

The operator-session revalidation on the same request (`resolve_active_session`) is a
three-table indexed join on primary keys — not a candidate either.

## Eliminated: the app being globally slow

Comparing an nginx-served asset against a FastAPI-proxied route on one warm connection,
from a host whose network floor to the origin is ~0.29 s:

```text
/assets/main-*.js  (nginx static)   ttfb=0.295
/healthz           (FastAPI)        ttfb=0.289
/api/config        (FastAPI, 401)   ttfb=0.288
```

FastAPI answers as fast as nginx serves a file. The process is not starved at idle, and the
unauthenticated path carries no hidden cost. Whatever costs 2 s is either specific to the
authenticated path or load-dependent.

## Leading hypothesis: head-of-line blocking behind MCP work

The console serves everything from **one uvicorn process, one event loop**
(`app.py:594`, no `workers`), and that loop also hosts the FastMCP `/mcp` surface. The API
container is capped at **`cpu: 500m`** with **`requests: 50m`** (`deployment.yaml:332-338`), so
under contention the pod is throttled toward a twentieth of a core.

The expensive thing sharing that loop is MCP reflection. Per this package's README, a
`tools/list` pays a full connect (`initialize`, `tools/list`, teardown) to _every_ configured
server, `stateless_http=True` means there is no session to amortize it over, the fan-out is
concurrent so a listing costs its slowest upstream, and an upstream addressed by its public URL
hairpins out through the Gateway. `mcp_reflection_cache.py` bounds this to once per 60 s per
`(server, config, credential)` — which means a reflection storm is _expected_ every minute a
client is connected, not an anomaly.

An `/api/approvals/pending` request landing in one of those windows waits behind it. That fits
the symptom shape: fast at idle, ~2 s intermittently, unrelated to queue size.

Circumstantial support: the Claude session doing this investigation had the Haku connector
reported `connected` + `enabledInChat`, yet **none** of its tools were present — the documented
signature of a server that was degraded or timed out during connect-time discovery.

**Not yet checked** (needs cluster access): `container_cpu_cfs_throttled_seconds_total` on the
`haku-console` pods, actual authenticated TTFB, and whether the 2 s correlates with reflection.

## Aggravating factor, independent of the above

`/api/approvals/pending` is refetched **in full** far more often than the data changes.
`useConsoleEvents` (`frontend/console_events.ts`) calls `sync()` on mount, on every WS event, on
socket open, on socket close, **and on a 30-second timer** — from every open tab. The WebSocket
is invalidation-only: `ToolCallsChangedEvent` already carries the `tool_call_id` that changed
(`console_events.py`), and every recipient answers it by re-reading the whole queue over REST.

So each change costs one full list read per tab, and each idle tab costs one every 30 s. This
does not by itself explain 2 s, but it multiplies whatever the per-request cost turns out to be,
and it is the load that would drive the contention above.

Note the constraint on the obvious fix: Postgres `NOTIFY` payloads are capped at 8000 bytes, so
the record cannot simply ride the existing LISTEN/NOTIFY channel. Each replica would have to
read the changed record once and push it to its own sockets — one indexed primary-key read per
event per replica, instead of one full-list scan per tab.

## Next step

Measure on the pod before changing anything: authenticated TTFB, CFS throttling, and whether
slow requests coincide with MCP reflection. If throttling is confirmed, the CPU limit and the
single-loop colocation of `/mcp` with the browser API are the levers — not the query.
