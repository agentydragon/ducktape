# auragon_study_casino

Single-user habit-tracking "casino" — study for a session, earn credits,
spend credits on self-chosen prizes. Frontend is a React PWA installable
on Windows (Edge/Chrome) and iPhone (Safari → Add to Home Screen).

Lives at <https://casino.allegedly.works>.

## Layout

| Path                                   | What it is                                             |
| -------------------------------------- | ------------------------------------------------------ |
| `app.py`                               | FastAPI backend: `/state` GET/PUT + static PWA mount   |
| `config.py`                            | Pydantic settings (DATA_DIR, host, port)               |
| `storage.py`                           | SQLite state store with content-addressed ETag         |
| `test_app.py`, `test_storage.py`       | pytest smoke tests                                     |
| `frontend/src/study_casino.jsx`        | The React component (originally a claude.ai artifact)  |
| `frontend/src/storage.js`              | Offline-first state sync (IndexedDB + backend PUT)     |
| `frontend/src/main.jsx`                | Entry — renders into `#root`, registers service worker |
| `frontend/index.html`                  | App shell (manifest link, theme color, apple-\* meta)  |
| `frontend/manifest.webmanifest`        | PWA manifest                                           |
| `frontend/sw.js`                       | Service worker (cache app shell, network-only /state)  |
| `frontend/icon.svg`                    | App icon                                               |
| `frontend/esbuild.config.mjs`          | Production bundler config                              |
| `BUILD.bazel` / `frontend/BUILD.bazel` | Bazel wiring                                           |

## Auth

Forward-auth via Authentik's embedded proxy outpost. The backend reads
`X-Authentik-Username` purely for logging; there is no per-user scoping
because this is a single-user app. See
`cluster/k8s/authentik/app/blueprints/study-casino-sso.yaml` for the
proxy provider, and
`cluster/k8s/authentik/proxy-routes/study-casino-httproute.yaml` for the
public hostname route.

## State sync

The frontend writes to IndexedDB immediately for snappy UX, then pushes
to the backend's `PUT /state` with an `If-Match` ETag. On 412 (another
device wrote since our last read) the frontend pulls the remote copy and
reloads — "last device to edit wins", which is the right trade-off for
a single user with two devices and no expectation of simultaneous edits.

The service worker serves the app shell offline but is configured to
bypass the cache for `/state` so a stale GET can't defeat cross-device
sync.

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
