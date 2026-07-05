# haku/console — Haku's interactive console

A small FastAPI service that serves the Haku console as a **React single-page app
over a JSON API**. It is the trusted operator interface: Authentik operator-only,
reviewed ducktape code. It replaced the static nginx + git-sync dashboard (now retired).

All product surfaces have **moved** to `haku/state_template/ui/` — Haku's own UI service
(`haku-ui`), which the console frames **full-page** as a sandboxed cross-origin iframe. The
console is now just the trusted outer shell: the capability tier (launch-routine) plus the
bridge that brokers the iframe's privileged requests. It holds no haku-state write credential.

**Trust boundary:** the console is reviewed/released ducktape code, so it runs in
its **own `haku-console` namespace** — deliberately _not_ `haku-sandbox`, the
namespace Haku has full CRUD over. Haku therefore has no RBAC to read the console's
secrets/logs or patch it, and the console sits outside the `haku-egress-proxy` egress
fence (that fence keys on `haku-sandbox`). This is the confidentiality boundary that
lets the console hold secrets Haku may not read (e.g. the Claude Code web session
bearer). Haku's full security model
(threat model, enforcement inventory, invariants): <../docs/security.md>.

## The capability tier — privileged actions, operator-gated

The console exposes exactly one privileged surface: the **capability tier**
(`capabilities.py`, `/api/capabilities/*`). It uses console-only secrets and acts on the
world, so it's gated hard (see <../docs/security.md> → enforcement inventory #11):

- **CSRF-gated.** A header-located double-submit token: the SPA fetches it from
  `GET /api/capabilities/csrf` (which also sets the signed cookie) and echoes it in
  `X-CSRF-Token` on the POST — so a cross-site request can't ride the operator's
  Authentik session cookie to fire a capability.
- **Server-side secret.** The bearer is read from `Settings` / the
  `haku-routine-launch-token` secret and attached to the upstream call; it never
  reaches the client.
- **Audited.** Every invocation logs to stdout in the `haku-console` namespace, which
  Haku has no RBAC to read.
- **Tiny, PR-gated allowlist.** Today one capability: `POST /api/capabilities/launch-routine`
  fires the Haku claude-code-web routine via its public Anthropic fire URL, optionally with
  per-run `text`. Adding a verb is a ducktape PR, never runtime data.

Firing must be a genuine operator gesture against **trusted-rendered chrome**: the
agent-authored iframe can only _request_ a launch (`requestLaunch` over the bridge); the
shell renders its own confirm (showing the prompt verbatim) and only then fires. So agent
UI can ask for the capability but can never script or spoof it.

There is **no** low-privilege "trace" write tier anymore — operator feedback now writes
straight into haku-state from haku-ui (which Haku already owns), so the console needs no
haku-state git credential or clone at all.

## Free-form UI — Haku's own UI, embedded

The console frames Haku's own UI service (`haku-ui.allegedly.works`, a separate
Authentik-gated app Haku runs in `haku-sandbox`) as a **full-page sandboxed cross-origin
iframe** — it never renders or even sees the iframe's content. `HAKU_CONSOLE_HAKU_UI_URL`
enables it; the response CSP adds `frame-src` for that origin and Authentik's origin
(`HAKU_CONSOLE_AUTH_ORIGIN`, default `https://auth.allegedly.works`) so the in-frame SSO
redirect can complete.
Containment is cross-origin isolation: the iframe can't read the console's DOM/cookies or
act as it. The trusted **bridge** (`bridge.ts`) lets the iframe _request_ three things via
postMessage — opening a link (`openLink`), launching a run (`requestLaunch`), and reading
the operator's location (`requestGeolocation`, gated by a shell-owned standing consent grant
since the iframe has no `allow="geolocation"`); the shell origin-checks, schema-validates,
and decides/confirms before acting. It also mirrors the iframe's hash route (`routeChanged`,
validated as a path) into the console's own URL fragment so refresh and deep links restore
the view. A persistent ⚙ escape button opens the shell's own console panel
(`console_panel.tsx`) — trusted chrome hosting shell-owned controls like the
location-sharing withdraw. See <docs/containment.md>.

## Layout

| Path               | Role                                                                                                                                                                                                                                |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`           | FastAPI `create_app`. `GET /api/config`, `GET /healthz`, CSRF config, mounts the capability router. It can serve the SPA for local/direct fallback when `HAKU_CONSOLE_STATIC_DIR` is set.                                           |
| `capabilities.py`  | Capability-tier router (`/api/capabilities/*`): CSRF-gated, audited privileged actions. `POST /launch-routine` fires the routine with the server-side bearer and optional per-run text; `GET /csrf` issues the double-submit token. |
| `models.py`        | Pydantic `ConfigResponse` — the `/api/config` response model.                                                                                                                                                                       |
| `config.py`        | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                                    |
| `export_schema.py` | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                                     |
| `frontend/`        | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                              |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change); the `haku-console` namespace itself is `cluster/k8s/haku/console-namespace/`.
The deployment runs two containers in one pod: the `haku-console` FastAPI API
image and a separate `haku-console-static` nginx image that bakes in the
fingerprinted SPA. nginx serves `/` and `/assets/*`, proxies `/api/*` and
`/healthz` to FastAPI on localhost, and sets cache policy by route (`/assets/*`
immutable, app shell revalidated, API/health uncached). No runtime asset copy or
shared web volume is used.
Non-root, dropped caps, no service-account token. Credentials: just the
`haku-routine-launch-token` secret (the capability tier's bearer; `HAKU_CONSOLE_LAUNCH_ROUTINE__TOKEN`).
It no longer holds a haku-state git credential — feedback/trace writes moved into haku-ui.
As trusted ducktape code in its own namespace it is **not** behind the `haku-egress-proxy`
fence — it gets ordinary cluster egress (which the capability tier needs to reach the
Anthropic fire URL). Security model: `haku/docs/security.md`; roadmap: `haku/PLAN.md` and the
`haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
