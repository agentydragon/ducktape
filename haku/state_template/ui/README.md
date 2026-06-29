# `ui/` — Haku's own item UI service (CI-built starter)

Self-contained app Haku runs in `haku-sandbox`, served behind the operator-owned
Authentik-gated `haku-ui.allegedly.works` route and embedded in the trusted console's
**Free-form UI** iframe. It is the **ported** item UI (from `haku/console/`), now
Haku-owned: Haku adopts it into its `haku-state` repo and evolves it freely.

This is **starter source only**. It is NOT wired into ducktape's Bazel build — the
build artifact is a container image produced by **Forgejo CI** (`.forgejo/workflows/build-ui.yaml`),
never a committed `dist/`. `ducktape bbr test` does not cover it; its own tests run in
the Forgejo CI **test gate** (see _Tests_ below).

## Pieces

| Path                                  | Role                                                                                                                                                                                                                                                              |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend/`                           | React + TypeScript SPA (standard Vite). Value-ranked **Up next** (top 7) + collapsible **Backlog**, collapsible task cards, `marked`+`dompurify` markdown bodies, command-action click/un-click toggles, `claude_handoff` deep-links, per-item + global feedback. |
| `backend/`                            | FastAPI app (no build step). Talks to the **Forgejo contents API** (no local clone): reads `items/`+`clicks/`, serves the SPA + a JSON API, and writes operator intent (`clicks/`, `intake/`) back to `haku-state`.                                               |
| `Dockerfile`                          | Multi-stage: `frontend` builds the SPA → `backend-base` installs deps → `test` runs pytest → `runtime` runs uvicorn on `:8080` as non-root with the built SPA copied in.                                                                                          |
| `backend/test_*.py`                   | Backend pytest suite (models parsing, the Forgejo read path, the FastAPI endpoints). Run during the CI test gate; dev-only deps in `backend/requirements-dev.txt`.                                                                                                |
| `../.forgejo/workflows/build-ui.yaml` | Forgejo Actions workflow: **test** (gate) → **build** push `git.allegedly.works/haku/ui:main-<utc>-<sha>`. Flux image automation (not CI) then writes the tag into `../k8s/haku-ui/deployment.yaml`.                                                              |
| `../k8s/haku-ui/`                     | Deployment (the built image, `haku-forgejo-registry-pull` imagePullSecret, `haku-state-git-write` env) + Service (`80` → `8080`).                                                                                                                                 |

## Frontend

Standard Vite React+TS — no Mantine/Tailwind/Bazel (lean and self-contained). Plain
CSS in `src/styles.css`.

```bash
cd frontend
npm install
npm run build   # → dist/  (the Dockerfile copies this into the backend's static dir)
npm run dev     # local dev server; proxies /api to a backend on :8080
```

Because this UI runs **inside** the console's sandboxed iframe (no `allow-popups`),
it cannot open links itself. All outbound navigation (`claude_handoff`, item source)
goes through the console's **`openLink` postMessage bridge**: `src/bridge.ts` posts
`{type:"openLink", url}` to `window.parent` (origin `https://haku.allegedly.works`);
the trusted shell scheme-gates, whitelists/confirms, and opens it. The shell side is at
`haku/console/frontend/bridge.ts`.

## Backend

FastAPI, port `8080`. Endpoints:

- `GET /api/dashboard` — items + currently-clicked actions + last scan time.
- `PUT/DELETE /api/trace/items/{id}/actions/{aid}` — record/retract a click.
- `POST /api/trace/feedback` — append an intake note (`text`, optional `item_id`).
- `GET /healthz`.

Each write is **one Forgejo contents-API commit** (the server makes it — no local
clone, no push/reconcile loop; see `backend/forgejo.py`), writing the **exact**
conventions Haku reduces: `clicks/<item-id>/<action-id>` and
`intake/<ts>-feedback[-<id>].md`. There is no capability tier (the privileged
launch-routine stays in the trusted console); this is the low-privilege trace surface
only.

Operator auth: the app is reachable only via the Authentik outpost, which injects
`X-authentik-username`. The backend reads it to log who acted. Trusting it for
authorization requires the ingress NetworkPolicy restricting the app to the outpost
(Phase 3 hardening) — until then it is advisory.

Config is env-driven (`HAKU_UI_*`): `GIT_USERNAME`/`GIT_PASSWORD` from the
`haku-state-git-write` secret (used as Forgejo basic auth), `FORGEJO_API_URL` the
internal Forgejo API root
(`http://forgejo-http.forgejo:3000/api/v1/repos/haku/haku-state`), `STATIC_DIR` the
bundled SPA.

```bash
cd backend
pip install -r requirements.txt
HAKU_UI_GIT_USERNAME=… HAKU_UI_GIT_PASSWORD=… HAKU_UI_FORGEJO_API_URL=… python app.py
```

## Tests

The CI **test gate** runs before anything is built for real or pushed, so no untested
image ever ships:

- **Backend** (`backend/test_*.py`, pytest): item schema parsing (discriminated unions,
  unknown-field tolerance, invalid-enum rejection), the Forgejo read path (`read_dashboard`
  tree+blob parsing and click derivation, mocked with `httpx.MockTransport`), and the
  FastAPI endpoints (health, dashboard, improvements, trace writes via a fake Forgejo). They run
  in the Dockerfile's `test` stage (`RUN python -m pytest -q`), so a failure fails the
  `docker build` and the whole gate.
- **Frontend**: `tsc -b` runs as part of `npm run build` in the `frontend` stage, so a
  type error fails the build too.

Run the backend tests locally:

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

## Build + deploy flow (Forgejo CI)

1. Haku commits `ui/` source + the workflow to `haku-state`.
2. The repo-scoped, contained Forgejo Actions runner (`cluster/k8s/haku-ci`) runs the
   **test** job (the gate above); only if it passes does the **build** job build the
   image and push `git.allegedly.works/haku/ui:main-<utc>-<sha>` to the in-cluster
   registry. CI stops here — it never edits a manifest.
3. Flux image automation (operator-owned, in ducktape `cluster/k8s/...`) watches the
   registry, picks the newest tag, and writes it into `k8s/haku-ui/deployment.yaml`
   at the `{"$imagepolicy": ...}` marker.
4. Flux reconciles `haku-state` `k8s/` → the `haku-ui` Deployment rolls the new image.

Runtime deps land separately: the runner (`cluster/k8s/haku-ci`) and the
`haku-forgejo-registry-pull` imagePullSecret. The registry **push** authenticates as the
repo owner with the `REGISTRY_PUSH_TOKEN` repo Action secret (Forgejo's built-in
`github.token` cannot push packages yet — forgejo#3571).


