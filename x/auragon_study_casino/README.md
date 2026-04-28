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
Clients (the Yjs `Y.Doc` running in the React PWA) sync their local
doc via `POST /sync`, sending base64-encoded
`{state_vector_b64, update_b64}`:

- Server: clones canonical → applies the client update on the trial
  → runs every validator → on success promotes trial to canonical
  and persists, then returns the binary diff the client still needs;
  on failure leaves canonical untouched and returns a 409 with a
  `{rejection: {rule, message}}` payload.
- Client: applies the returned server update, surfaces a toast for
  rejections, and rolls back the offending transaction via
  `Y.UndoManager` so the local Doc always matches canonical.

Document shape — mirrored verbatim between `doc_shape.py` (server)
and `frontend/src/sync.js` (client):

```
balance   : Y.Map { credits: number, tokens: number }
active    : Y.Map (current live session, or empty when none)
sessions  : Y.Map[id, Y.Map { subject, seconds, ended_at_ms }]
prizes    : Y.Map[id, Y.Map { name, cost }]
prize_log : Y.Array[Y.Map { id, name, cost, at_ms }]
```

CRDTs guarantee convergence but not business rules — the server's
validators (credits ≥ 0, tokens ≥ 0, prize shape, session shape) are
the only thing keeping the casino's economy honest. See
<validators.py> for the rule list and the rejection contract.

The service worker serves the app shell offline but bypasses the
cache for `/sync` so a stale GET can't defeat cross-device sync.
While offline, mutations stay in the local Y.Doc + IndexedDB and
replay on reconnect; the SyncBanner UI surfaces "offline" and any
rejection so failures are never silent.

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
