# Free-form agent UI via a Haku-run iframe service

Status: **design / not started.** This is the concrete shape for Phase 1b of
`haku/PLAN.md` → _The agent-authored console_ — letting Haku author increasingly
free-form interactive UI, without the trusted console having to render or even
understand it.

## The shift

Earlier sketches had the console **render** Haku's UI (ship a transpiler, run
Haku's TSX in a sandboxed iframe, relay source/data over `postMessage`). This
drops all of that. Instead:

> Haku runs **its own UI service** in `haku-sandbox` and serves whatever it wants.
> The trusted console **embeds it in a cross-origin iframe** and does nothing else
> with it. The console never sees Haku's UI code, framework, or build.

The trusted console shrinks to its irreducible job: **hold the launch bearer, own
privileged actions, and frame Haku's UI.** Everything visual — and the reading and
writing of `haku-state` for display and operator intent — moves into Haku's
service, which already runs inside Haku's own perimeter.

```
 haku.allegedly.works            TRUSTED shell (ducktape, Authentik, holds the bearer)
   • chrome + Launch button (capability tier: CSRF, bearer, shell-owned)
   • <iframe src="https://haku-ui.allegedly.works">   ← cross-origin, same-site
        │
        ▼
 haku-ui.allegedly.works         operator-owned Authentik-gated route (PR-gated)
        │   → Service (haku-sandbox)
        ▼
   Haku's UI Deployment + pods   HAKU-OWNED (haku-sandbox CRUD)
   • serves whatever UI Haku wants (its choice of framework/runtime)
   • reads/writes haku-state itself (it already has the creds)
   • sees the operator identity from the Authentik forward-auth headers
```

## Division of ownership

| Thing                                                                                                         | Owner               | PR-gated?                    |
| ------------------------------------------------------------------------------------------------------------- | ------------------- | ---------------------------- |
| Trusted shell: capability tier (bearer), iframe host, CSP, the protocol                                       | operator / ducktape | ✅                           |
| **Public route** `haku-ui.allegedly.works` (HTTPRoute + Authentik provider/blueprint) → the `haku-ui` Service | operator            | ✅                           |
| The `haku-ui` Service contract (name, port, pod label selector) in `haku-sandbox`                             | operator            | ✅                           |
| **The UI Deployment + pods + everything served**                                                              | **Haku**            | ❌ — Haku's to evolve freely |

The boundary lands exactly where it should: the operator owns _the frame, the
secret, and the public door_; Haku owns _the picture inside the frame_.

## Containment invariants (the load-bearing part)

The iframe content is **100% Haku-controlled and is assumed adversarial** (a
prompt-injected Haku could author hostile UI — see `haku/PLAN.md` doctrine, "the
container is the trust boundary"). Safety comes from the perimeter, not from
trusting the content:

1. **Cross-origin isolation from the shell.** `haku-ui` is a different origin than
   `haku`, so the iframe cannot read the shell's DOM, cookies, or `localStorage`,
   and cannot call the shell's capability API with the operator's credentials. The
   launch bearer is unreachable from any browser context. Use a **same-site**
   subdomain (`*.allegedly.works`) so it's _cross-origin but same-site_: isolated
   from the shell, yet the Authentik SSO cookie isn't treated as a blocked
   third-party cookie. Pin it with `sandbox="allow-scripts"` on the `<iframe>` and
   `frame-src https://haku-ui.allegedly.works` in the shell's CSP.

2. **Haku's UI is never publicly exposed.** Its only ingress is the
   operator-owned, **Authentik-gated** route. Haku cannot publish private
   information to the public through it, because the only door is gated to the
   operator.

3. **The agent cannot create public routes — keep it that way.** This is a cluster
   invariant, not specific to this UI:
   - **Today (RBAC):** Haku's full-CRUD Role is `haku-sandbox-admin` in
     `haku-sandbox` (bound to group `oidc-ksbx-groups:haku`; a sibling of the
     claude agent's `claude-sandbox-admin`, not the same role). It is an explicit
     resource allowlist (`pods`, `services`, `deployments`, `configmaps`,
     `secrets`, …) and does **not** include `httproutes`/`gateways`
     (`gateway.networking.k8s.io`), so Haku cannot attach a route to the public
     gateway. **Do not add those resources to the Role.**
   - **Defense-in-depth (recommended, not yet done):** the `cluster-gateway`
     listeners are `allowedRoutes: namespaces: from: All`
     (`cluster/k8s/gateway/gateway.yaml`). That is a latent hole — if any
     agent-writable namespace ever gained `httproutes` permission, it could
     self-expose. Tighten `allowedRoutes.namespaces` to a label `Selector` (or an
     explicit set) that agent namespaces never carry, so "agent can't create
     public routes" holds structurally at the gateway layer too.

4. **Worst case is bounded.** What a hostile iframe can do: render misleading UI,
   and read/write `haku-state` — both of which Haku can already do. It cannot
   exfiltrate to the public (invariant 2), cannot reach the bearer or fire a
   capability (invariant 1), and cannot open a new public door (invariant 3).
   Residual risk is **consent fidelity** — agent UI could phish the operator
   (fake "approve X" that records Y). Mitigations: keep privileged controls and
   their copy in the **shell chrome** (not the iframe), and mark the iframe region
   visibly as agent-authored.

## How Haku runs the service (no image builds)

Haku can't build/push container images (GHCR/CI is PR-gated), but it has full CRUD
in `haku-sandbox`. So it runs a **stock runtime image** and sources its app from
`haku-state` at runtime — the same "edit via git, no rebuild" model the console
itself uses. Concretely, Haku's Deployment is some public base image (a static
server, or a Node/Python runtime) with an init/sidecar that clones/pulls
`haku-state` and serves the app from a `ui/` dir. Haku evolves the UI by committing
to `haku-state`; the running service picks it up. The console does not care which
runtime or framework Haku chooses — it only frames the URL.

The operator pins the **Service contract** (name `haku-ui`, a port, a pod label
selector); Haku runs pods matching it. If Haku breaks its own Service, its UI is
down — self-inflicted, no security impact.

## Data and operator-intent flow

Because Haku's service has the `haku-state` creds, **data and intent do not go
through the shell**:

- **Display data** — Haku's backend reads `haku-state` (items, etc.) and serves it
  to its own UI. No shell involvement.
- **Operator intent** — a click in Haku's UI calls Haku's own backend (same-origin
  to the iframe), which writes `haku-state` (the equivalent of today's
  `clicks/`/`intake/`). Haku reduces it on its next run.
- **Operator identity** — Haku's backend reads the **Authentik forward-auth
  headers** (e.g. `X-authentik-username`) on requests to its gated route, so it
  knows the request is genuinely the operator. (Caveat: it must only trust those
  headers on traffic that arrived through the outpost, not direct in-cluster
  calls — standard forward-auth hygiene.)

This means the console's existing **trace tier becomes legacy**: it stays only for
the trusted declarative item list (Phase 1a). Once the UI lives in Haku's iframe,
Haku's own backend records intent, and the trace tier can eventually be retired.

## The shell in the fullscreen model

The iframe is expected to **fill the viewport** — Haku's UI scrolls internally like
any full-page app, so there's no content-height autosizing. The trusted shell is then
a **thin layer over** the iframe, not chrome around it. It owns, on its **own z-layer**
(which the agent UI can't draw over or intercept):

- **a persistent trust indicator** (small badge / border) so the operator can always
  tell shell pixels from agent-authored pixels — the only pixels to trust for a
  privileged prompt. This matters more fullscreen: the agent surface is a large
  phishing canvas.
- **privileged-action confirms** (below), rendered as overlays above the iframe.
- the frame + the bridge endpoint.

## The protocol (postMessage)

Data + intent go to Haku's backend directly, so the bridge carries only actions that
need the trusted side. Every inbound message is origin-checked
(`event.origin === "https://haku-ui.allegedly.works"`) and schema-validated; the
iframe can only _request_ — the shell decides and acts. Two first affordances:

### `requestCapability` — perform a shell-owned action

`{type: "requestCapability", id: "launch-routine"}` → the shell pops its **own**
confirm overlay (trusted copy, CSRF) and fires with the bearer. The iframe never
holds the bearer or invokes directly. This is the cheapest first affordance — it
reuses the existing capability tier; the request just triggers the flow already there.

### `openLink` — send the operator to a URL

`{type: "openLink", url}` → the shell opens the link on the operator's behalf (the
iframe can't: tight sandbox + cross-origin). Rules:

- **Scheme is a hard gate** (never behind a confirm): allow only `https` (and
  `mailto`); reject `javascript:` / `data:` / `blob:` / `file:` / … outright —
  opening those in the top context would be code execution in the _shell's_ origin.
  "Arbitrary links" means arbitrary `https` hosts, not arbitrary schemes.
- **The host whitelist decides warn-vs-not.** It is **operator-owned trusted config
  in the shell** — _not_ in `haku-state`, or Haku could whitelist a phishing host and
  skip the confirm:
  - whitelisted (claude.ai, github.com, your own services, …) → open directly;
  - otherwise → shell **confirm overlay showing the real, full URL** → Open / Cancel
    (a consent + anti-phishing gate: the agent supplies the URL, the shell displays it
    honestly).
- Always `window.open(url, "_blank", "noopener,noreferrer")`.

This subsumes the item→handoff loop (a `claude.ai/new` deep-link is just a whitelisted
open) and gives Haku a general "send me to a link" verb without ever letting agent UI
navigate the operator unsupervised.

**Popup permission (assumed one-time setup).** A `window.open` relayed over
`postMessage` loses user-activation and gets popup-blocked. Rather than engineer
around it, the operator grants a one-time per-origin **"allow pop-ups for
`haku.allegedly.works`"**; thereafter the shell opens freely from postMessage
handlers. The permission is on the **shell** origin only — the iframe stays
`sandbox="allow-scripts"` (no `allow-popups`), so it still cannot open anything itself;
only the trusted shell opens, and only after the scheme gate + whitelist/confirm.
(Chrome/Firefox honor the persistent per-site allow cleanly; Safari is fiddlier —
fine for a single-operator Chrome/Firefox tool.)

## Phasing

- **v1 — frame it + the minimal bridge.** Operator stands up the Authentik-gated
  same-site route → the `haku-ui` Service in `haku-sandbox`, the fullscreen iframe
  (`sandbox` + CSP + trust indicator), and ships `requestCapability("launch-routine")`
  - `openLink`. Haku stands up its Deployment and serves a first page reading
    `haku-state`. Goal: prove the frame + isolation + the two affordances end-to-end.
- **v1.5 — gateway hardening.** Tighten `cluster-gateway` `allowedRoutes` so the
  "no agent public routes" invariant holds at the gateway, not only via RBAC.
- **later — more capabilities; retire the trace tier / declarative list** once the
  agent-authored UI fully subsumes it.

## Open questions

- **Service contract specifics** — exact Service name/port/label, and whether the
  operator also provides a baseline Deployment skeleton or leaves the whole
  workload to Haku.
- **Authentik in an iframe** — confirm the same-site SSO cookie + outpost
  forward-auth works cleanly inside the iframe without an in-frame login redirect;
  if not, fall back to bridging display data from the shell and keeping Haku's
  service un-gated of operator data.
- **Forward-auth header trust** — how Haku's backend distinguishes outpost traffic
  from direct in-cluster calls (so header identity can't be spoofed by another
  `haku-sandbox` pod).
- **Visible agent-authored boundary** — how the shell frames the iframe so the
  operator always knows in-iframe content is agent-authored (anti-phishing).
