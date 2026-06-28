# haku/console — Haku's interactive console

A small FastAPI service that serves the Haku console as a **React single-page app
over a JSON API**. It is the trusted operator interface: Authentik operator-only,
reviewed ducktape code. It replaced the static nginx + git-sync dashboard (now retired).

Item rendering has **moved** to `haku/state_template/ui/` — Haku's own UI service,
embedded via a sandboxed cross-origin iframe. The console is now a thin shell:
the capability tier (launch-routine) + a generic "Note to Haku" trace box + the
Free-form UI iframe.

**Trust boundary:** the console is reviewed/released ducktape code, so it runs in
its **own `haku-console` namespace** — deliberately _not_ `haku-sandbox`, the
namespace Haku has full CRUD over. Haku therefore has no RBAC to read the console's
secrets/logs or patch it, and the console sits outside the `haku-mitmproxy` egress
fence (that fence keys on `haku-sandbox`). This is the confidentiality boundary that
lets the console hold secrets Haku may not read (e.g. the Claude Code web session
bearer). See `haku/PLAN.md` → _The agent-authored console_.

## Trace tier — item-agnostic

The console's trace tier is **item-agnostic**. `POST /api/trace` accepts a plain
`{text: str}` and appends it as an intake note (`intake/<timestamp>-trace.md`) to
haku-state, then commit-pushes. The frontend constructs whatever text it needs (e.g.
"item blah action blah") and sends it as an opaque string. No verbs beyond "write a
note" live in the trace tier; Haku reduces the notes on its next run.

The backend keeps the haku-state clone and the `haku-state-git-write` secret so it
can author these commits. There is no read path (no items, no clicks, no pull loop) —
the clone is write-only from the console's perspective.

## Two write tiers — the internal security split

Writes are split by **what's the worst case if agent-authored UI made this call with
no real operator behind it** (see `haku/PLAN.md` → _The agent-authored console_):

- **Trace tier** (`trace.py`, `POST /api/trace`) — only records operator-expressed intent
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

## Free-form UI — Haku's own UI, embedded

The console embeds Haku's own UI service (`haku-ui.allegedly.works`, a separate
Authentik-gated app Haku runs in `haku-sandbox`) as a **sandboxed cross-origin iframe** —
the console never renders or even sees its content. `HAKU_CONSOLE_HAKU_UI_URL` enables it;
when set, the response CSP adds `frame-src` for that origin and Authentik's origin
(`HAKU_CONSOLE_AUTH_ORIGIN`, default `https://auth.allegedly.works`) so the in-frame SSO
redirect can complete.
Containment is cross-origin isolation: the iframe can't read the console's DOM/cookies
or act as it — so Haku's UI can't act as the console. The trusted **bridge** (`bridge.ts`)
lets the iframe request link opens via postMessage; the shell origin-checks, schema-validates,
and decides. See `console/plans/free_form_ui_iframe.md`.

## Layout

| Path               | Role                                                                                                                                                                                                                  |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`           | FastAPI `create_app` + lifespan (clone). `GET /api/config`, `GET /healthz`, CSRF config, mounts the trace + capability routers. It can serve the SPA for local/direct fallback when `HAKU_CONSOLE_STATIC_DIR` is set. |
| `trace.py`         | Trace-tier router (`/api/trace`): a single `POST` that records an opaque operator note to haku-state. Low-privilege haku-state write; reads `git_state` off `app.state`.                                              |
| `capabilities.py`  | Capability-tier router (`/api/capabilities/*`): CSRF-gated, audited privileged actions. `POST /launch-routine` fires the routine with the server-side bearer; `GET /csrf` issues the double-submit token.             |
| `git_state.py`     | pygit2 clone of haku-state; `reconcile` (fetch + hard-reset), `commit_push` with retry, `append_trace` (the single write path). Talks to the cluster-internal **plaintext-HTTP** Forgejo (no TLS/CA needed).          |
| `models.py`        | Pydantic `TraceRequest` and `ConfigResponse` — the `/api` request/response models.                                                                                                                                    |
| `config.py`        | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                      |
| `export_schema.py` | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                       |
| `frontend/`        | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change); the `haku-console` namespace itself is `cluster/k8s/haku/console-namespace/`.
The deployment runs two containers in one pod: the `haku-console` FastAPI API
image and a separate `haku-console-static` nginx image that bakes in the
fingerprinted SPA. nginx serves `/` and `/assets/*`, proxies `/api/*` and
`/healthz` to FastAPI on localhost, and sets cache policy by route (`/assets/*`
immutable, app shell revalidated, API/health uncached). No runtime asset copy or
shared web volume is used.
Non-root, dropped caps, no service-account token. Credentials: the `haku-state-git-write`
secret (provisioned into `haku-console` by the `tf/gitops/haku-state` module) and the
`haku-routine-launch-token` secret (the capability tier's bearer; `HAKU_CONSOLE_LAUNCH_ROUTINE__TOKEN`).
As trusted ducktape code in its own namespace it is **not** behind the `haku-mitmproxy`
fence — it gets ordinary cluster egress (which the capability tier needs to reach the
Anthropic fire URL). Design + roadmap: `haku/PLAN.md` and the
`haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
