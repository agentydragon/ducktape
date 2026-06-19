# haku/console — Haku's interactive dashboard

A small FastAPI service that serves Haku's dashboard as a **React single-page app
over a JSON API**, reading items from a clone of the `haku-state` repo and writing
operator actions back as git commits. It is the **Haku console**: an interface Haku
builds for the operator to interact _with Haku_, running with **exactly Haku's
perimeter** — read-only to the world, write only to the internal `haku-state`
Forgejo, Authentik operator-only — and never anything more. It replaced the static
nginx + git-sync dashboard (now retired).

## Action model — the backend stays dumb

The console never interprets what an action _means_. Each item Haku writes can carry
`actions[]` (e.g. _Snooze 30d_, _Draft the email_, _Research deeper_); the dashboard
renders each `command` action as a **click/un-click toggle**. Clicking records an
overlay file `clicks/<item-id>/<action-id>` (`POST /api/items/<id>/actions/<aid>`),
un-clicking removes it (`DELETE`) — each a commit by the `haku-console` identity.
**Haku reduces the overlay on its next run**: it reads the clicked actions, carries
out each one's intent, and clears the click. So new verbs need no backend change —
Haku invents the action and its meaning; the console only records the click.
`claude_handoff` actions are stateless `claude.ai/new` deep-links (no commit). The
global feedback box appends to `intake/`.

## Boundary

- The backend (git layer, FastAPI JSON API) and frontend (React SPA) are **tested
  ducktape code**, built Bazel→GHCR→Flux and deployed in the `haku-sandbox` namespace.
- It is **driven by haku-state at runtime for content**: items (data) are read from
  the clone, so Haku evolves _what_ the dashboard shows without an image rebuild. The
  _look_ now lives in the bundle (a frontend rebuild changes it) — unlike the old
  server-rendered console, whose page/CSS templates could be overridden from the clone.

## Layout

| Path               | Role                                                                                                                                                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `app.py`           | FastAPI `create_app` + lifespan (clone + background pull loop). JSON API: `GET /api/dashboard`, `POST`/`DELETE /api/items/{id}/actions/{aid}`, `POST /api/feedback`, `GET /healthz`; serves the SPA (`StaticFiles`) otherwise. |
| `git_state.py`     | pygit2 clone of haku-state; `reconcile` (fetch + hard-reset), `commit_push` with retry, `read_items`, and the clicks/-overlay + feedback writers. Talks to the cluster-internal **plaintext-HTTP** Forgejo (no TLS/CA needed). |
| `models.py`        | Pydantic `Item` (discriminated-union `action` + `actions[]`) and the `/api` request/response models.                                                                                                                           |
| `config.py`        | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                               |
| `export_schema.py` | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                                |
| `frontend/`        | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                         |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change). Non-root, dropped caps, no service-account token; the only
credential is the `haku-state-git-write` secret; egress is the existing
`haku-sandbox` mitmproxy policy (no new NetworkPolicy). The image bundles the built
SPA at `/app/web` (`HAKU_CONSOLE_STATIC_DIR`). Design + roadmap: `haku/PLAN.md` and
the `haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
