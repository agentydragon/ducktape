# haku/console — Haku's interactive dashboard

A small FastAPI service that serves Haku's dashboard as a **React single-page app
over a JSON API**, reading items from a clone of the `haku-state` repo and writing
operator actions back as git commits. It is the **Haku console**: an interface Haku
builds for the operator to interact _with Haku_, Authentik operator-only, read-only
to the world, writing only to the internal `haku-state` Forgejo. It replaced the
static nginx + git-sync dashboard (now retired).

**Trust boundary:** the console is reviewed/released ducktape code, so it runs in
its **own `haku-console` namespace** — deliberately _not_ `haku-sandbox`, the
namespace Haku has full CRUD over. Haku therefore has no RBAC to read the console's
secrets/logs or patch it, and the console sits outside the `haku-mitmproxy` egress
fence (that fence keys on `haku-sandbox`). This is the confidentiality boundary that
lets the console later hold secrets Haku may not read (e.g. the Claude Code web
session bearer). See `haku/PLAN.md` → _The agent-authored console_.

## Action model — the backend stays dumb

The console never interprets what an action _means_. Each item Haku writes can carry
`actions[]` (e.g. _Snooze 30d_, _Draft the email_, _Research deeper_); the dashboard
renders each `command` action as a **click/un-click toggle**. Clicking records an
overlay file `clicks/<item-id>/<action-id>` (`PUT /api/trace/items/<id>/actions/<aid>`),
un-clicking removes it (`DELETE`) — each a commit by the `haku-console` identity.
**Haku reduces the overlay on its next run**: it reads the clicked actions, carries
out each one's intent, and clears the click. So new verbs need no backend change —
Haku invents the action and its meaning; the console only records the click.
`claude_handoff` actions are stateless `claude.ai/new` deep-links (no commit).
Feedback — the global box, or a per-item box on each card — appends to `intake/`
(`POST /api/trace/feedback`); per-item notes are tagged with the item id, which Haku
reduces as feedback on that item.

## Two write tiers — the internal security split

Writes are split by **what's the worst case if agent-authored UI made this call with
no real operator behind it** (see `haku/PLAN.md` → _The agent-authored console_):

- **Trace tier** (`trace.py`, `/api/trace/*`) — only records operator-expressed intent
  into `haku-state`, which Haku already owns, so it grants the agent **nothing new**.
  Low-privilege; safe to expose to agent-authored UI.
- **Capability tier** (`capabilities.py`, `/api/capabilities/*`) — uses console-only
  secrets and acts on the world. The teeth live here, so it's treated very differently:
  - **CSRF-gated.** A header-located double-submit token: the SPA fetches it from
    `GET /api/capabilities/csrf` (which also sets the signed cookie) and echoes it in
    `X-CSRF-Token` on the POST — so a cross-site request can't ride the operator's
    Authentik session cookie to fire a capability.
  - **Server-side secret.** The bearer is read from `Settings` / the
    `haku-routine-launch-token` secret and attached to the upstream call; it never
    reaches the client.
  - **Audited.** Every invocation logs to stdout in the `haku-console` namespace, which
    Haku has no RBAC to read.
  - **Tiny, PR-gated allowlist.** Today one capability: `POST
/api/capabilities/launch-routine` fires the Haku claude-code-web routine via its
    public Anthropic fire URL. Adding a verb is a ducktape PR, never runtime data.

  The split is legible in the code: a search for what touches a privileged secret
  returns `capabilities.py` and never the trace router.

## Content vs. look

Driven by `haku-state` **at runtime for content**: items (data) are read from the clone,
so Haku evolves _what_ the dashboard shows without an image rebuild. The _look_ now lives
in the bundle (a frontend rebuild changes it) — unlike the old server-rendered console,
whose page/CSS templates could be overridden from the clone.

## Layout

| Path               | Role                                                                                                                                                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `app.py`           | FastAPI `create_app` + lifespan (clone + background pull loop). Read endpoints (`GET /api/dashboard`, `GET /healthz`), CSRF config, mounts the trace + capability routers, serves the SPA (`StaticFiles`) otherwise.           |
| `trace.py`         | Trace-tier router (`/api/trace/*`): the overlay toggle (`PUT`/`DELETE /api/trace/items/{id}/actions/{aid}`) and `POST /api/trace/feedback`. Low-privilege haku-state writes; reads `git_state` off `app.state`.                |
| `capabilities.py`  | Capability-tier router (`/api/capabilities/*`): CSRF-gated, audited privileged actions. `POST /launch-routine` fires the routine with the server-side bearer; `GET /csrf` issues the double-submit token.                      |
| `git_state.py`     | pygit2 clone of haku-state; `reconcile` (fetch + hard-reset), `commit_push` with retry, `read_items`, and the clicks/-overlay + feedback writers. Talks to the cluster-internal **plaintext-HTTP** Forgejo (no TLS/CA needed). |
| `models.py`        | Pydantic `Item` (discriminated-union `action` + `actions[]`) and the `/api` request/response models.                                                                                                                           |
| `config.py`        | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                               |
| `export_schema.py` | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                                |
| `frontend/`        | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                         |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change); the `haku-console` namespace itself is `cluster/k8s/haku/console-namespace/`.
Non-root, dropped caps, no service-account token. Credentials: the `haku-state-git-write`
secret (provisioned into `haku-console` by the `tf/gitops/haku-state` module) and the
`haku-routine-launch-token` secret (the capability tier's bearer; `HAKU_CONSOLE_LAUNCH_ROUTINE__TOKEN`).
As trusted ducktape code in its own namespace it is **not** behind the `haku-mitmproxy`
fence — it gets ordinary cluster egress (which the capability tier needs to reach the
Anthropic fire URL). The image bundles the built SPA at `/app/web`
(`HAKU_CONSOLE_STATIC_DIR`). Design + roadmap: `haku/PLAN.md` and the `haku-state` repo's
`plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
