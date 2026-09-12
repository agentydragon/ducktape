# A shared push mechanism for `UISHELL_DRAWER` and `NO_MANUAL_REFRESH`

Status: **captured, not yet confirmed.** Both `UISHELL_DRAWER` (task DAG) and `NO_MANUAL_REFRESH`
need a push/subscription mechanism, and both were flagged as "not decided here" pending this design.
Read this before starting either.

## What exists today: two independent implementations, one of them not generic

**`live.py` + `live.tsx`** is a real, reusable primitive already:

- `Changes` (`app/changes.py`) is a bare asyncio pub/sub: `subscribe(waiter)` registers an
  `asyncio.Event`, `notify()` sets every registered waiter. No payload, no queue — a burst of
  changes coalesces into one wakeup.
- `frames(snapshot, health, *watched, interval_s)` (`live.py`) is a generic async generator: on
  each `watched` `Changes.notify()`, re-run `snapshot()` and yield it as an SSE `snapshot` frame;
  during quiet periods, yield a `health` frame every `interval_s`. Callers supply only a
  `snapshot()` closure and the `Changes` objects relevant to it.
- `useLive<T>(url)` (`live.tsx`) is the matching frontend half: one `EventSource`, `snapshot`/
  `health`/`error` handling, a `connecting`/`connected`/`disconnected` state machine, and
  `LiveStatus` for rendering the "stream is stale" banner.

This pattern already serves `/live/sandboxes` and `/live/sandboxes/{name}` — both backed by an
in-process Kubernetes watch (`LiveIndex`) plus the trajectory store's own `Changes`.

**The Action Service's `/actions/stream`** is a second, hand-rolled implementation of the same
idea, not built on the above:

- `action_service/updates.py`'s `ActionUpdates` is a PostgreSQL-`LISTEN`-backed fan-out, narrowly
  shaped for Actions: `subscribe(request_id)` for one caller's bounded wait, `subscribe_all()` for
  server-push consumers. The channel name (`agentplane_action_updates`) and the per-request keying
  are baked in.
- The NOTIFY itself is a SQLAlchemy `after_insert` listener on `ActionEventRow` (`action_service/db.py`)
  — since every Action lifecycle transition inserts a canonical event row, one listener covers
  create/decide/execute/etc. without scattering `pg_notify` calls across write paths.
- `action_service/api.py`'s `operator_stream` hand-rolls its own snapshot-then-keepalive loop
  (`subscribe_all()`, re-list on wake, `: keepalive` comment frames on a 5s timeout) — the same shape
  as `live.py`'s `frames()`, duplicated rather than reused, because it lives in a different process
  (the Action Service, not the app) and has no shared import path to `live.py` today.
- `x/agentplane/app/api.py`'s `/actions/stream` doesn't generate frames at all; it proxies the
  Action Service's raw SSE bytes through (`_action_chunks`), re-checking the operator session on
  each chunk.
- The frontend side (`actions.tsx`'s `useActionRequests`) is its own third EventSource client: no
  `health`/staleness reporting, no `connecting` state, errors folded into the existing `error`
  string instead of `LiveStatus`.

**Everything `UISHELL_DRAWER` and `NO_MANUAL_REFRESH` need lives behind the Action Service**, not
`LiveIndex`: pending Actions (already streamed), and three resources with _no_ change notification
today — Connections (`external_connection`/`external_connection_grant` tables), MCP-server linkage
(`mcp_server_linkage`/`mcp_oauth_token_state`), and push subscriptions (`action_push_subscription`).
Each has its own canonical table, the same shape `ActionEventRow` is for Actions.

## Proposed design

**1. Generalize the PostgreSQL-NOTIFY fan-out.** Turn `ActionUpdates` into a reusable
`channel: str`-parametrized class (`PgNotifyFanout` or similar) in a shared location both the
Action-specific bounded-wait code and new resource streams can use. The per-request `subscribe(id)`
path stays Action-specific (nothing else needs it); `subscribe_all()` generalizes directly. Add one
`after_insert`/`after_update` SQLAlchemy listener per new resource's canonical table, each `NOTIFY`ing
its own channel — mirroring `ActionEventRow`'s listener exactly, not a shared channel for everything
(a Connections change waking Push-subscription readers, or vice versa, would be a lot of pointless
re-fetching for tabs that show unrelated data).

**2. Stop duplicating the snapshot-loop.** Move `live.py`'s `frames()` (or an equivalent) somewhere
the Action Service can import too — it's already fully generic (`snapshot`, `health`, `*watched:
Changes`) and needs no change to serve Connections/MCP-linkage/Push-subscription streams once each
has its own `Changes`-shaped subscription. `operator_stream`'s hand-rolled loop should be rewritten
against it rather than left as a fourth divergent copy once this exists — a small cleanup this design
enables but does not itself require doing first.

**3. New Action Service + app-proxy streams**, one per resource, following the exact shape
`/actions` → `/actions/stream` already establishes: `/v1/operator/connections/stream`,
`/v1/operator/mcp-servers/stream`, `/v1/operator/push/subscriptions/stream` in the Action Service,
each proxied through a matching `x/agentplane/app/api.py` route the way `/actions/stream` proxies
`operator_stream` today.

**4. One shared Actions subscription, not one per consumer.** `UISHELL_DRAWER`'s badge needs the
same pending-Actions data `actions.tsx`'s `useActionRequests` already streams — it does not need a
new backend mechanism, only to stop being read exclusively by route-scoped page components. Lift the
`/actions/stream` subscription into a provider mounted once in `app.tsx` (a context, not a hook each
consumer calls independently), so there is exactly one `EventSource` per tab regardless of how many
of {the badge, `/actions`, `/actions/history`} are mounted. `actions.tsx`'s existing hand-rolled
EventSource client should migrate onto `live.tsx`'s `useLive`/`LiveStatus` in the same pass — one
frontend implementation of "subscribe to a snapshot stream," not two.

**5. `NO_MANUAL_REFRESH`'s three Settings tabs** each call `useLive` once against their new stream
route, replacing today's fetch-once-plus-Refresh-button. No shared subscription needed between them
— each tab is independently mounted and unmounted with the Settings modal, so a separate
`EventSource` per open tab is the same cost a normal page navigation already pays elsewhere in the
app (e.g. moving from `/sandboxes` to a sandbox page).

## Open questions, not settled here

- **Bundled vs. separate streams for the three Settings resources.** One `/v1/operator/settings/stream`
  carrying all three vs. three independent endpoints. Bundling means fewer connections but couples
  an MCP-linkage change to waking the Connections tab's reader too, and complicates the payload
  shape (three independently-typed lists rather than one). Leaning separate, matching each tab's own
  lifecycle, but not decided.
- **Badge payload shape.** Whether `UISHELL_DRAWER` reads the exact same `ActionRequestView[]`
  `/actions/stream` already carries (simplest, reuses the shared subscription verbatim) or a lighter
  count-plus-summary projection (less bandwidth/parse cost per frame, but a second Action Service
  endpoint and a second frontend shape to keep in sync with the full list). Leaning reuse-as-is
  first, add a lighter projection only if the full payload proves too heavy in practice.
- **Where `PgNotifyFanout` and the moved `frames()` helper live.** Both the app and the Action
  Service need them; whether that's a new shared module both import, or the Action Service keeps its
  own copy synchronized by convention, is a build-graph question this doc doesn't resolve.
- **Migrating `operator_stream` onto the shared `frames()` helper** is enabled by this design but is
  its own follow-up, not a prerequisite for `UISHELL_DRAWER`/`NO_MANUAL_REFRESH` to ship.
