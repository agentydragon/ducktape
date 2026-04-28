# auragon_study_casino

Single-user habit-tracking "casino" — study for a session, earn credits,
gamble those credits in the casino for tokens, then spend tokens on
self-chosen prizes. Credits → tokens is one-way (via the casino or an
explicit conversion), so winnings can never be re-gambled. Frontend is
a React PWA installable on Windows (Edge/Chrome) and iPhone (Safari →
Add to Home Screen).

Lives at <https://casino.allegedly.works>.

## Layout

| Path                                   | What it is                                                 |
| -------------------------------------- | ---------------------------------------------------------- |
| `app.py`                               | FastAPI backend: `POST /sync`, `/healthz`, static frontend |
| `config.py`                            | Pydantic settings (DATA_DIR, host, port)                   |
| `doc_shape.py`                         | Casino Y.Doc schema (mirror of frontend/src/sync.js)       |
| `validators.py`                        | Post-merge constraint checks (credits ≥ 0, prize shape…)   |
| `models.py`                            | SQLAlchemy `doc` row holding the binary Y update blob      |
| `store.py`                             | DocStore: validate-then-persist client updates             |
| `test_doc_shape.py`                    | pycrdt API + Casino schema sanity                          |
| `test_validators.py`                   | Per-rule rejection coverage                                |
| `test_store.py`                        | DocStore round-trip + persistence + validation gate        |
| `test_app.py`                          | HTTP-surface coverage of `/sync`                           |
| `tests/test_sync_two_device.py`        | Two-client multi-device sync E2E (pycrdt-driven)           |
| `frontend/src/study_casino.jsx`        | The React component (originally a claude.ai artifact)      |
| `frontend/src/sync.js`                 | Y.Doc + y-indexeddb + HTTP poll provider against `/sync`   |
| `frontend/src/y_hooks.js`              | useYMap / useYArray / useSyncStatus React hooks            |
| `frontend/src/use_casino.js`           | Single hook exposing reactive state + every mutation       |
| `frontend/src/SyncBanner.jsx`          | Header banner + toast for offline/rejected/syncing states  |
| `frontend/src/main.jsx`                | Entry — renders into `#root`, registers service worker     |
| `frontend/index.html`                  | App shell (manifest link, theme color, apple-\* meta)      |
| `frontend/manifest.webmanifest`        | PWA manifest                                               |
| `frontend/sw.js`                       | Service worker (cache app shell, network-only `/sync`)     |
| `frontend/icon.svg`                    | App icon                                                   |
| `frontend/esbuild.config.mjs`          | Production bundler config                                  |
| `BUILD.bazel` / `frontend/BUILD.bazel` | Bazel wiring                                               |

## Auth

OIDC Authorization Code flow (confidential client). The backend (`auth.py`)
handles `/auth/login` → Authentik → `/auth/callback`, then issues an
HMAC-signed session cookie. Per-user SQLite databases scope state to the
authenticated user. Authentik resources (OAuth2 provider, application, policy
bindings) are managed by TF at
`tf/gitops/sso-providers/provider_study_casino.tf`.

## State sync

Y-CRDT, server-authoritative validation. The server holds one Y.Doc
in memory and persists it as a single binary update blob in SQLite.
Clients sync via a persistent WebSocket at `/ws` (JSON, both directions):

- Client → server: `{"type":"sync","state_vector_b64":"...","update_b64":"..."}`
- Server → client: `{"type":"accepted",...}` | `{"type":"rejected","rule","message"}`
  | `{"type":"server_push",...}` (fan-out to other tabs when one tab syncs)

On acceptance the client clears its `Y.UndoManager` undo stack so
already-synced changes can't be rolled back by a later rejection.
On rejection the stack (which only contains post-last-sync changes) is
drained, and the client pulls fresh state from the server.

Document shape — mirrored verbatim between `doc_shape.py` (server)
and `frontend/src/sync.js` (client):

```
balance   : Y.Map { credits: number, tokens: number }
sessions  : Y.Map[id, Y.Map] — all sessions, in-progress or completed
            in-progress (no ended_at_ms): { subject, start_time_ms,
              paused, paused_duration_ms, pause_started_at_ms }
            completed: { subject, seconds, ended_at_ms }
prizes    : Y.Map[id, Y.Map { name, cost }]
prize_log : Y.Array[Y.Map { id, name, cost, at_ms }]
active    : Y.Map — legacy, kept for one-time migration only
```

CRDTs guarantee convergence but not business rules — the server's
validators (credits ≥ 0, tokens ≥ 0, prize shape, session shape) are
the only thing keeping the casino's economy honest. See
<validators.py> for the rule list and the rejection contract.

## Build

```bash
bbr test //x/auragon_study_casino/...  //x/auragon_study_casino/tests/...
bbr build //x/auragon_study_casino:image
```

Local dev (requires node_modules linked via Bazel):

```bash
# Iterate on the frontend, auto-rebuild:
cd x/auragon_study_casino/frontend && node esbuild.config.mjs dist --watch

# Run the backend against the local dist:
bb run --remote_executor="" //x/auragon_study_casino:server
```
