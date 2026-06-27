# Free-form agent UI via a Haku-run iframe service

Status: **foundation shipped (2026-06-26); steps 1–4 remaining** — see
_Implementation order_. This is the concrete shape for Phase 1b of `haku/PLAN.md` →
_The agent-authored console_ — letting Haku author increasingly free-form interactive
UI, without the trusted console having to render or even understand it.

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
   third-party cookie. Pin it with `sandbox="allow-scripts allow-same-origin allow-forms"`
   on the `<iframe>` (same-origin + forms are needed for the framed app's own Authentik
   auth; **no `allow-popups`** — only the shell opens links) and
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
   - **Defense-in-depth:** the `cluster-gateway` listeners are `allowedRoutes:
namespaces: from: All` (`cluster/k8s/gateway/gateway.yaml`) — a latent hole if any
     agent-writable namespace ever gained `httproutes` permission. **Fenced** by the
     `restrict-agent-gateway-routes` Kyverno ClusterPolicy (denies route/Gateway
     creation in agent namespaces). The remaining gateway-layer belt-and-suspenders —
     tightening `allowedRoutes.namespaces` to a `Selector` agent namespaces never carry —
     is deferred (`cluster/k8s/TODO.md`).

4. **Worst case is bounded.** What a hostile iframe can do: render misleading UI,
   and read/write `haku-state` — both of which Haku can already do. It cannot
   exfiltrate to the public (invariant 2), cannot reach the bearer or fire a
   capability (invariant 1), and cannot open a new public door (invariant 3).
   Residual risk is **consent fidelity** — agent UI could phish the operator
   (fake "approve X" that records Y). Mitigation is the capability gate (a faked
   control is inert) plus the **top-layer modal confirm + backdrop** at the moment of
   a privileged action — _not_ a visible "agent-authored" marker, which the iframe can
   spoof. See "The shell as a thin trusted layer".

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

## The shell as a thin trusted layer

The iframe is expected to **cover most of the page** — Haku's UI scrolls internally
like a full-page app, so there's no content-height autosizing. The trusted shell is
then a **thin layer**, not chrome around it. It owns:

- **the moment-of-decision confirm** for any privileged action — see below. This, not
  a badge, is the trustworthy surface.
- the frame + the bridge endpoint + the CSP.

**A persistent "trust indicator" is not a security control — don't rely on it.** A web
page has no secure-attention channel (the OS has Ctrl-Alt-Del; the browser has the
address bar — neither is available to a sub-region of a page). An iframe covering most
of the page can render a pixel-perfect **decoy** badge; the shell's real badge sits on
a higher z-layer so it can't be drawn _over_, but the operator can't reliably tell it
from a decoy drawn beside it. A static frame/border is at most cosmetic honesty, never
a boundary.

Security therefore does **not** depend on the operator visually distinguishing shell
pixels from agent pixels. It rests on two things:

1. **The capability gate makes a faked control inert.** If the iframe draws a fake
   "Launch" button, clicking it only emits a `requestCapability` (below) — which the
   shell re-gates with its own CSRF + confirm + bearer. Spoofing the _look_ of a
   privileged control accomplishes nothing on the privileged path.
2. **The confirm is the only trustworthy surface, and it only has to exist at the
   moment of approval.** Render it as a **top-layer modal** (`<dialog>.showModal()`)
   with a **backdrop**: the iframe cannot draw over it, read it, or intercept clicks
   meant for it (top layer + cross-origin + pointer capture), and the backdrop dims
   the agent UI so "the shell is talking now" is unambiguous. Anti-clickjacking
   hygiene on it: explicit action text the operator must read, a deliberate click on a
   freshly-rendered button (no click-through / brief focus delay) — the residual
   attack isn't spoofing a badge, it's baiting the operator's click onto the real
   confirm.

Also **withhold the Fullscreen API** from the iframe (no `allow="fullscreen"`) so it
can't go truly fullscreen and spoof the browser chrome itself.

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

## Implementation order

**Shipped — the foundation (2026-06-26).** Gateway hardening (Kyverno denylist so agents
can't create public routes); the `haku-state` workloads Flux pipe (Haku's `k8s/` reconciled
into `haku-sandbox` under a constrained impersonation SA, `cluster/k8s/haku/workloads/`)
seeded from `haku/state_template/`; the Authentik-gated `haku-ui.allegedly.works` route; and
the console's **Free-form UI** tab embedding it as a sandboxed cross-origin iframe (`frame-src`
CSP, `sandbox="allow-scripts allow-same-origin allow-forms"`). The de-risking
question — _does a same-site Authentik-gated app authenticate inside the iframe?_ — is
**answered yes**: the proxied haku-ui content is frameable; the only blocker was Authentik's
flow page sending `X-Frame-Options: DENY`, fixed by serving
`Content-Security-Policy: frame-ancestors 'self' https://haku.allegedly.works` on
`auth.allegedly.works` so only the console may frame it. Cross-origin isolation holds, so
`haku-ui ≠ haku-console` regardless of framing headers.

Also shipped: the **`postMessage` bridge** (origin-checked transport + the top-layer
native-`<dialog>` confirm with backdrop) and the **`openLink`** affordance — scheme hard-gate
(`https`/`mailto` only), operator-owned host whitelist in the shell (`bridge.ts`, not
`haku-state`), off-whitelist confirm, and `window.open(…, "noopener,noreferrer")` (assumes the
one-time per-origin pop-up allow). The iframe sandbox dropped `allow-popups` accordingly — only
the shell opens. `state_template/k8s/haku-ui/index.html` carries a worked `openLink` demo.

**Remaining**, in order (each ~PR-sized; **owner** is who builds it):

1. **Haku's real UI service** _(Haku)._ Haku replaces the placeholder under its `k8s/`
   (stock runtime + `haku-state` code), serves a real first page (re-create the item list as
   a starting point), records operator intent to its **own** backend, reads operator identity
   from the forward-auth headers. Coexists with the trusted list; needs nothing from the shell
   yet.
2. **`requestCapability`** _(operator)._ The remaining bridge affordance: wire
   `requestCapability("launch-routine")` onto the already-shipped transport + top-layer confirm
   (reuses the existing capability tier — the request just triggers the flow already there).
3. **North star — subsume the item UI** _(last; see below)._ Once Haku's UI is at least as
   good as the trusted list, retire the trusted renderer + item schema + trace tier from
   ducktape, move styling/build/image-automation into `haku-state`, and shrink the shell to
   the irreducible core. Most disruptive; only safe once 1–2 are proven.

## North star: the shell owns nothing but the boundary

The end state (later — not the next step) drops the last thing ducktape still owns on
the _content_ side: the **item model and its UI**. Today the trusted console owns the
item schema (`models.py` / `base/schema/item.json`), the declarative renderer, the SPA
bundle + styling, and the trace tier. In the north star, **all of that moves into
Haku's iframe service and `haku-state`**:

- Haku is given a **high-level objective** — _"surface for the operator the things
  that are useful to act on"_ — and **no schema**. There is no ducktape-defined
  `Item`, no fixed action kinds, no prescribed layout. Haku decides what the
  abstraction is and how to present it, and evolves it freely.
- The frontend's **styling, compilation, and the console SPA's Flux image automation**
  move out of ducktape into **agent-owned `haku-state` code** — Haku's UI service
  builds and serves its own assets; no ducktape rebuild for a UI or schema change.
- The **trace tier retires** — Haku's own backend records operator intent (it already
  owns `haku-state`).

What's left in ducktape is the **irreducible trusted core, and nothing else**:

- the **capability tier** (the bearer + CSRF + `launch-routine` and any future
  privileged verbs);
- the **iframe host** page + the **`postMessage` protocol** and its validation;
- the **CSP** + the **top-layer modal confirm** (the only trustworthy surface, at the moment of a privileged action);
- the **perimeter** (`haku-console` namespace, the Authentik-gated route, the
  gateway-route invariant).

The litmus test for "does this belong in ducktape": **does it hold a secret, perform a
privileged action, or define the trust boundary?** If not, it's Haku's — and the item
abstraction fails that test, so it goes.

Deliberately deferred: this only makes sense once the iframe UI + the affordances are
proven and Haku can author an experience at least as good as today's declarative list.
Until then, Phase 1a's trusted item list coexists.

## Open questions

- **Share the iframe protocol instead of duplicating it.** The protocol contract is
  small, so for now Haku's UI keeps a hand-maintained copy of the message shapes,
  with `haku/console/frontend/bridge.ts` as the authoritative source (notes in both
  copies). They can drift. Share it properly later — a tiny package both pin, or a
  generated/sync-checked artifact.
- **Consider building Haku's UI with Bazel sometime.** It's currently a standalone
  app (Vite + FastAPI + Dockerfile, built by Forgejo CI with rootless buildkit) —
  ordinary tooling, deliberately _not_ Bazel, since the build is a small agent-owned
  app, must stay off BuildBuddy (private), and should be easy for Haku to iterate on.
  If we later want ducktape's build consistency + lint aspects + the shared protocol
  as a real `bazel_dep`, revisit a Bazel build (the gaffer-private pattern: a
  `haku-state` Bazel workspace on pinned ducktape, built local-only in the runner).
- **Forward-auth header trust** — how Haku's backend distinguishes outpost traffic
  from direct in-cluster calls (so header identity can't be spoofed by another
  `haku-sandbox` pod).
- **Visible agent-authored boundary** — how the shell frames the iframe so the
  operator always knows in-iframe content is agent-authored (anti-phishing).
