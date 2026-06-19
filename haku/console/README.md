# haku/console — Haku's interactive dashboard

A small FastAPI service that renders Haku's dashboard from a clone of the
`haku-state` repo and writes operator actions back as git commits. It is the
**Haku console**: an interface Haku builds for the operator to interact _with
Haku_, running with **exactly Haku's perimeter** — read-only to the world, write
only to the internal `haku-state` Forgejo, Authentik operator-only — and never
anything more. It replaced the static nginx + git-sync dashboard (now retired).

## Action model — the backend stays dumb

The console never interprets what an action _means_. Each item Haku writes can carry
`actions[]` (e.g. _Snooze 30d_, _Draft the email_, _Research deeper_); the
dashboard renders each `command` action as a **click/un-click toggle**. Clicking
records an overlay file `clicks/<item-id>/<action-id>` (`POST`), un-clicking
removes it (`…/unclick` for plain-form posts, or `DELETE`) — each a commit by the
`haku-console` identity. **Haku reduces the overlay on its next run**: it reads the
clicked actions, carries out each one's intent, and clears the click. So new verbs
need no backend change — Haku invents the action and its meaning; the console only
records the click. `claude_handoff` actions are stateless `claude.ai/new`
deep-links (no commit). The global feedback box appends to `intake/`.

## Boundary

- This engine (renderer, git layer, FastAPI app) is **tested ducktape code**, built
  Bazel→GHCR→Flux and deployed in the `haku-sandbox` namespace.
- It is **driven by haku-state at runtime**: items (data) and, when present,
  presentation overrides (`dashboard/templates/{page.html.j2,style.css}`) are read
  from the clone, so Haku evolves the look + content without an image rebuild. The
  loader **fails safe** to the baked defaults if an override is missing or broken.

## Layout

| File                                 | Role                                                                                                                                                                                                             |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `renderer.py`                        | Markdown→HTML + per-item card rendering (ported from haku-state `dashboard/generate.py`) + `render_page`.                                                                                                        |
| `templates/` + `templates_loader.py` | Baked default Jinja page + CSS, override-able from the clone (fail-safe).                                                                                                                                        |
| `git_state.py`                       | pygit2 clone of haku-state; `reconcile` (fetch + hard-reset), `commit_push` with retry, and the clicks/-overlay + feedback writers. Talks to the cluster-internal **plaintext-HTTP** Forgejo (no TLS/CA needed). |
| `config.py`                          | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                 |
| `app.py`                             | FastAPI `create_app` + lifespan (clone + background pull loop). Routes: `GET /`, `GET /healthz`, `POST /items/{id}/actions/{action}` (+ `/unclick`, `DELETE`), `POST /feedback`.                                 |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change). Non-root, dropped caps, no service-account token; the only
credential is the `haku-state-git-write` secret; egress is the
existing `haku-sandbox` mitmproxy policy (no new NetworkPolicy). Design + roadmap:
`haku/PLAN.md` and the `haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
