# auragon_study_casino

Single-user habit-tracking "casino" — study for a session, earn credits,
gamble those credits in the casino for tokens, then spend tokens on
self-chosen prizes. Credits → tokens is one-way (via the casino or an
explicit conversion), so winnings can never be re-gambled. Frontend is
a React PWA installable on Windows (Edge/Chrome) and iPhone (Safari →
Add to Home Screen).

Lives at <https://casino.allegedly.works>.

## Layout

| Path                                   | What it is                                                |
| -------------------------------------- | --------------------------------------------------------- |
| `app.py`                               | FastAPI backend: `/state` GET, `/events` POST/GET, static |
| `config.py`                            | Pydantic settings (DATA_DIR, host, port)                  |
| `models.py`                            | SQLAlchemy models: `events` log + `snapshot` cache        |
| `reducer.py`                           | Pure reducer: fold events into state                      |
| `store.py`                             | EventStore: append events, re-reduce snapshot, ETag       |
| `test_reducer.py`                      | Per-event-type reducer semantics                          |
| `test_store.py`                        | EventStore round-trip + ETag concurrency                  |
| `test_app.py`                          | HTTP-surface integration tests                            |
| `frontend/src/study_casino.jsx`        | The React component (originally a claude.ai artifact)     |
| `frontend/src/storage.js`              | Thin event-append client (GET /state, POST /events)       |
| `frontend/src/main.jsx`                | Entry — renders into `#root`, registers service worker    |
| `frontend/index.html`                  | App shell (manifest link, theme color, apple-\* meta)     |
| `frontend/manifest.webmanifest`        | PWA manifest                                              |
| `frontend/sw.js`                       | Service worker (cache app shell, network-only /state)     |
| `frontend/icon.svg`                    | App icon                                                  |
| `frontend/esbuild.config.mjs`          | Production bundler config                                 |
| `BUILD.bazel` / `frontend/BUILD.bazel` | Bazel wiring                                              |

## Auth

Forward-auth via Authentik's embedded proxy outpost. The backend reads
`X-Authentik-Username` purely for logging; there is no per-user scoping
because this is a single-user app. See
`cluster/k8s/authentik/app/blueprints/study-casino-sso.yaml` for the
proxy provider, and
`cluster/k8s/authentik/proxy-routes/study-casino-httproute.yaml` for the
public hostname route.

## State sync

Event-sourced. The server stores an append-only `events` log plus a cached
`snapshot` row. Every user action emits one or more events via
`POST /events`; the server appends them, re-reduces the snapshot, and
returns the authoritative state. Clients use `If-Match` on the snapshot
ETag for optimistic concurrency — on 412 (another device wrote since our
last read) the client reloads; last-device-wins, no merge. Credits,
tokens, sessions, and the prize log are all derived from events — the
only non-derivable state is the current prize catalog (events:
`prize_added`, `prize_deleted`).

Event types: `session_{started,paused,resumed,completed,cancelled,edited,deleted}`,
`roulette_spin`, `slot_spin`, `blackjack_hand`, `prize_redeemed`,
`prize_added`, `prize_deleted`, `credits_delta`, `tokens_delta`,
`credits_to_tokens`, `import`, `reset`. The full reducer lives in
`reducer.py` (Python source of truth); the React frontend emits events
but does not re-reduce locally.

`GET /events?since_id=N&limit=M` returns the raw log (paginated) for
debugging and future analytics UIs.

The service worker serves the app shell offline but bypasses the cache
for `/state` so a stale GET can't defeat cross-device sync.

## Build

```bash
bbr test //x/auragon_study_casino/...
bbr build //x/auragon_study_casino:image
```

Local dev (requires node_modules linked via Bazel):

```bash
# Iterate on the frontend, auto-rebuild:
cd x/auragon_study_casino/frontend && node esbuild.config.mjs dist --watch

# Run the backend against the local dist:
bb run --remote_executor="" //x/auragon_study_casino:server
```
