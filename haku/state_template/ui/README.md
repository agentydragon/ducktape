# `ui/` — Haku's own item UI service (CI-built starter)

Self-contained app Haku runs in `haku-sandbox`, served behind the operator-owned
Authentik-gated `haku-ui.allegedly.works` route and embedded in the trusted console's
**Free-form UI** iframe. It is the **ported** item UI (from `haku/console/`), now
Haku-owned: Haku adopts it into its `haku-state` repo and evolves it freely.

This is **starter source only**. It is NOT wired into ducktape's Bazel build — the
build artifact is a container image produced by **Forgejo CI** (`.forgejo/workflows/build-ui.yaml`),
never a committed `dist/`. `ducktape bbr test` does not cover it.

## Pieces

| Path                                  | Role                                                                                                                                                                                                                                                              |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend/`                           | React + TypeScript SPA (standard Vite). Value-ranked **Up next** (top 7) + collapsible **Backlog**, collapsible task cards, `marked`+`dompurify` markdown bodies, command-action click/un-click toggles, `claude_handoff` deep-links, per-item + global feedback. |
| `backend/`                            | FastAPI app (no build step). Clones/pulls `haku-state`, serves the SPA + a JSON API, and writes operator intent (`clicks/`, `intake/`) back to `haku-state`.                                                                                                      |
| `Dockerfile`                          | Multi-stage: node builds the SPA → python:3.13-slim runtime runs uvicorn on `:8080` as non-root, with the built SPA copied in.                                                                                                                                    |
| `../.forgejo/workflows/build-ui.yaml` | Forgejo Actions workflow: build → push `git.allegedly.works/haku/ui:main-<utc>-<sha>`. Flux image automation (not CI) then writes the tag into `../k8s/haku-ui/deployment.yaml`.                                                                                  |
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
the trusted shell scheme-gates, whitelists/confirms, and opens it. See the demo in
`../k8s/haku-ui/index.html` (removed once this real UI ships) and the shell side at
`haku/console/frontend/bridge.ts`.

## Backend

FastAPI, port `8080`. Endpoints:

- `GET /api/dashboard` — items + currently-clicked actions + last scan time.
- `PUT/DELETE /api/trace/items/{id}/actions/{aid}` — record/retract a click.
- `POST /api/trace/feedback` — append an intake note (`text`, optional `item_id`).
- `GET /healthz`.

It writes the **exact** conventions Haku reduces: `clicks/<item-id>/<action-id>` and
`intake/<ts>-feedback[-<id>].md`. There is no capability tier (the privileged
launch-routine stays in the trusted console); this is the low-privilege trace surface
only.

Operator auth: the app is reachable only via the Authentik outpost, which injects
`X-authentik-username`. The backend reads it to log who acted. Trusting it for
authorization requires the ingress NetworkPolicy restricting the app to the outpost
(Phase 3 hardening) — until then it is advisory.

Config is env-driven (`HAKU_UI_*`): `GIT_USERNAME`/`GIT_PASSWORD` from the
`haku-state-git-write` secret, `GIT_REPO_URL` the internal Forgejo
(`http://forgejo-http.forgejo:3000/haku/haku-state.git`), `STATIC_DIR` the bundled SPA.

```bash
cd backend
pip install -r requirements.txt
HAKU_UI_GIT_USERNAME=… HAKU_UI_GIT_PASSWORD=… HAKU_UI_GIT_REPO_URL=… python app.py
```

## Build + deploy flow (Forgejo CI)

1. Haku commits `ui/` source + the workflow to `haku-state`.
2. The repo-scoped, contained Forgejo Actions runner (`cluster/k8s/haku-ci`) builds
   the image and pushes `git.allegedly.works/haku/ui:main-<utc>-<sha>` to the
   in-cluster registry. CI stops here — it never edits a manifest.
3. Flux image automation (operator-owned, in ducktape `cluster/k8s/...`) watches the
   registry, picks the newest tag, and writes it into `k8s/haku-ui/deployment.yaml`
   at the `{"$imagepolicy": ...}` marker.
4. Flux reconciles `haku-state` `k8s/` → the `haku-ui` Deployment rolls the new image.

Runtime deps land separately: the runner (`cluster/k8s/haku-ci`), the registry push
cred (`FORGEJO_REGISTRY_PASSWORD` Actions secret), and the `haku-forgejo-registry-pull`
imagePullSecret. Real end-to-end validation happens in the paving loop after those land.
