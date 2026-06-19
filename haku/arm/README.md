# haku/arm — interactive dashboard "arm"

A small FastAPI service that renders Haku's dashboard from a clone of the
`haku-state` repo and (later milestones) writes operator actions back as git
commits. It is an **"arm" of Haku**: an interface Haku builds for the operator to
interact _with Haku_, running with **exactly Haku's perimeter** — read-only to the
world, write only to the internal `haku-state` Forgejo, Authentik operator-only —
and never anything more. It replaces the static `haku-dashboard` (nginx + git-sync).

## Boundary

- This engine (renderer, git layer, FastAPI app) is **tested ducktape code**, built
  Bazel→GHCR→Flux and deployed in the `haku-sandbox` namespace.
- It is **driven by haku-state at runtime**: items (data) and, when present,
  presentation overrides (`dashboard/templates/{page.html.j2,style.css}`) are read
  from the clone, so Haku evolves the look + content without an image rebuild. The
  loader **fails safe** to the baked defaults if an override is missing or broken.

## Layout

| File                                 | Role                                                                                                                                                                    |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `renderer.py`                        | Markdown→HTML + per-item card rendering (ported from haku-state `dashboard/generate.py`) + `render_page`.                                                               |
| `templates/` + `templates_loader.py` | Baked default Jinja page + CSS, override-able from the clone (fail-safe).                                                                                               |
| `git_state.py`                       | pygit2 clone of haku-state; `reconcile` (fetch + hard-reset) and `commit_push` with retry. Talks to the cluster-internal **plaintext-HTTP** Forgejo (no TLS/CA needed). |
| `config.py`                          | Env settings (`HAKU_ARM_*`).                                                                                                                                            |
| `app.py`                             | FastAPI `create_app` + lifespan (clone + background pull loop) + routes (`GET /`, `GET /healthz`).                                                                      |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/arm/` (operator-owned — the perimeter is not
Haku's to change). Non-root, dropped caps, no service-account token; the only
credential is the `haku-state-git-write` secret; egress is the
existing `haku-sandbox` mitmproxy policy (no new NetworkPolicy). Design + roadmap:
`haku/PLAN.md` and the `haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/arm/...
```
