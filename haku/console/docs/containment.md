# Containment contract — embedding Haku-authored UI

The realized design for the agent-authored console: the trusted shell frames Haku's own UI
service and owns nothing but the boundary. This is the **current contract**, in present
tense — the design narrative and build history live in git (the retired
`plans/free_form_ui_iframe.md`). Haku's full security model (threat model, enforcement
inventory, invariants) is <../../docs/security.md>; this doc is the console-side detail
behind its inventory rows #10–#11.

## The shape

> Haku runs **its own UI service** in `haku-sandbox` and serves whatever it wants. The
> trusted console **embeds it in a cross-origin iframe** and does nothing else with it.
> The console never sees Haku's UI code, framework, or build.

```text
 haku.allegedly.works            TRUSTED shell (ducktape, Authentik, holds the bearer)
   • capability tier (CSRF, bearer, shell-owned)
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

| Thing                                                                                                          | Owner               | PR-gated?                    |
| -------------------------------------------------------------------------------------------------------------- | ------------------- | ---------------------------- |
| Trusted shell: capability tier (bearer), iframe host, CSP, the bridge protocol                                 | operator / ducktape | ✅                           |
| **Public route** `haku-ui.allegedly.works` (HTTPRoute + injected CSP + Authentik provider) → Service `haku-ui` | operator            | ✅                           |
| The `haku-ui` Service contract (name, port, pod label selector) in `haku-sandbox`                              | operator            | ✅                           |
| **The UI Deployment + pods + everything served**                                                               | **Haku**            | ❌ — Haku's to evolve freely |

The operator owns _the frame, the secret, and the public door_; Haku owns _the picture
inside the frame_. Haku ships the UI end-to-end from `haku-state`: Forgejo CI builds and
pushes the image, Flux image automation rolls it out (see
<../../../cluster/k8s/haku/workloads/README.md>); no ducktape rebuild is involved.

## Containment invariants (the load-bearing part)

The iframe content is **100% Haku-controlled and is assumed adversarial** (a prompt-injected
Haku could author hostile UI). Safety comes from the perimeter, not from trusting the content:

1. **Cross-origin isolation from the shell.** `haku-ui` is a different origin than `haku`,
   so the iframe cannot read the shell's DOM, cookies, or `localStorage`, and cannot call the
   shell's capability API with the operator's credentials — the launch bearer is unreachable
   from any browser context. The subdomain is **same-site** (`*.allegedly.works`): isolated
   from the shell, yet the Authentik SSO cookie isn't a blocked third-party cookie. Pinned by
   `sandbox="allow-scripts allow-same-origin allow-forms"` (**no `allow-popups`**, no
   `allow="fullscreen"`) and the shell CSP's `frame-src` for `haku-ui.allegedly.works` +
   `auth.allegedly.works` (the latter so in-frame SSO can complete; Authentik serves
   `frame-ancestors 'self' https://haku.allegedly.works` on `auth.allegedly.works`).
2. **No exfiltration to the public.** Inbound: haku-ui's only ingress is the operator-owned
   Authentik-gated route, so nothing it serves is publicly readable. Outbound from the
   operator's browser: the route **injects a strict CSP** on every haku-ui response
   (`connect-src 'self'`, …), fencing silent subresource beacons from outside Haku's write
   scope; navigation is fenced by the shell's `frame-src` + the sandbox + the `openLink`
   gate (below). Outbound from the backend: the `haku-egress-proxy` egress allowlist. Channel
   table: <../../docs/security.md> § _Browser-side exfiltration_.
3. **The agent cannot create public routes.** Haku's `haku-sandbox-admin` Role omits
   `httproutes`/`gateways` (do not add them), and the `restrict-agent-gateway-routes`
   Kyverno ClusterPolicy denies route/Gateway creation in agent namespaces even if RBAC
   drifted. Remaining belt-and-suspenders (listener `allowedRoutes` selector) is deferred —
   `cluster/k8s/TODO.md`.
4. **Worst case is bounded.** A hostile iframe can render misleading UI and read/write
   `haku-state` — both of which Haku can already do. Residual risk is **consent fidelity**
   (agent UI phishing the operator's click); the mitigations are the capability gate (a
   faked control is inert) and the top-layer confirm (below) — _never_ a visible
   "agent-authored" badge, which the iframe can spoof.

## The shell as a thin trusted layer

The iframe covers the page; the shell owns only the moment-of-decision confirm, the frame,
the bridge, and the CSP. **A persistent "trust indicator" is not a security control**: a web
page has no secure-attention channel, and an iframe can render a pixel-perfect decoy badge.
Security rests on two things instead:

1. **The capability gate makes a faked control inert.** A fake "Launch" button can only emit
   a bridge request, which the shell re-gates with its own CSRF + confirm + bearer.
2. **The confirm is the only trustworthy surface, and only at the moment of approval.** It
   renders as a **top-layer `<dialog>.showModal()` with a backdrop**: the iframe cannot draw
   over it, read it, or intercept its clicks. Anti-clickjacking hygiene: explicit action
   text, a deliberate click on a freshly-rendered button, no click-through.

Fullscreen is withheld from the iframe (no `allow="fullscreen"`) so it can't spoof browser
chrome.

## The bridge protocol (postMessage)

Display data and operator intent go straight to Haku's backend (it holds the `haku-state`
creds); the bridge carries only actions needing the trusted side. Every inbound message is
origin-checked (`event.origin === "https://haku-ui.allegedly.works"`) and schema-validated;
the iframe can only _request_ — the shell decides and acts. Authoritative implementation:
<../frontend/bridge.ts> (Haku's UI keeps a hand-maintained copy; sharing the contract is a
tracked cleanup in `haku/PLAN.md` → _Not yet built_).

### `requestLaunch` — fire the launch-routine capability

haku-ui renders its own launch dialog and posts `{type: "requestLaunch", id, prompt}`; the
shell pops its top-layer confirm showing the prompt **verbatim**, and only then fires with
the server-side bearer (see <../README.md> → _The capability tier_).

### `openLink` — send the operator to a URL

`{type: "openLink", url}` → the shell opens it on the operator's behalf (the sandbox has no
popups). Rules:

- **Scheme is a hard gate** (never behind a confirm): only `https` (and `mailto`);
  `javascript:`/`data:`/`blob:`/`file:` are rejected outright — opening those top-level
  would be code execution in the _shell's_ origin.
- **The host whitelist decides warn-vs-not**, and it is **operator-owned config in the
  shell** (`bridge.ts`), never `haku-state`: whitelisted hosts open directly; anything else
  gets a shell confirm showing the real, full URL.
- Open a same-origin blank tab first (missing handle = popup-block signal), sever `opener`,
  then navigate. The shell serves `Referrer-Policy: no-referrer`; `noopener`/`noreferrer`
  in the `window.open` feature string is avoided deliberately (those make it return `null`
  even on success).
- **Popup permission is a one-time per-origin setup** on the **shell** origin ("allow
  pop-ups for `haku.allegedly.works`") — postMessage-relayed opens lose user activation.
  The iframe still cannot open anything itself.

### `routeChanged` — mirror the route for refresh/deep-links

haku-ui posts `{type: "routeChanged", path}` on hash-route changes; the shell
`history.replaceState`s the path into its **own** URL fragment, and on load carries its
fragment back into the frame `src` so F5 / a deep link restores the view. Rules:

- **The path is never a URL.** `isRoutePath` (bridge.ts) enforces a leading `/` (not
  `//`), a conservative charset, and a length cap; the shell only ever puts the value in
  a fragment, and the restored `src` is always `uiUrl` with only its fragment replaced —
  the frame origin cannot be steered.
- Fragments never reach servers, so the mirrored route leaks nothing to the SSO hop and
  survives the in-frame Authentik 302 chain when a session exists.

## Operator identity — forward-auth headers, made trustworthy

Haku's backend reads `X-authentik-username` on requests arriving through its gated route.
Those headers are only forgeable by a direct in-cluster call to the Service — which the
`haku-ui-ingress-authentik-only` CiliumNetworkPolicy
(<../../../cluster/k8s/haku/namespace/networkpolicy.yaml>) blocks: ingress to `haku-ui` is
admitted only from the authentik-server pods running the outpost.
