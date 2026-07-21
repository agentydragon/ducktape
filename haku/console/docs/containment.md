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
   shell's capability API or Operator-authenticated `/mcp` path. Both reject the iframe's Origin
   and require the shell origin's inaccessible CSRF token; the launch bearer is unreachable from
   any browser context. The subdomain is **same-site** (`*.allegedly.works`): isolated
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

The iframe fills the content area beside a narrow shell-owned navigation rail; the shell owns only the moment-of-decision surfaces, the frame,
the bridge, and the CSP. **A persistent "trust indicator" is not a security control**: a web
page has no secure-attention channel, and an iframe can render a pixel-perfect decoy badge.
Security rests on two things instead:

1. **The capability gate makes a faked control inert.** A fake "Launch" button can only emit
   a bridge request, which the shell re-gates with its own CSRF + confirm + bearer.
2. **The shell is the only trustworthy approval surface.** Immediate bridge escalations render as
   a **top-layer `<dialog>.showModal()` with a backdrop**. Queueable approvals render in the
   shell-owned non-modal approvals drawer. In both cases the iframe cannot draw over the trusted controls,
   read them, or intercept their clicks. Anti-clickjacking hygiene: explicit action text, a
   deliberate click on a freshly-rendered button, no click-through.

Fullscreen is withheld from the iframe (no `allow="fullscreen"`) so it can't spoof browser
chrome.

**The shell chrome.** A persistent shell-owned left rail reserves layout space the frame cannot
render into. It navigates among the still-mounted Haku UI frame and trusted Settings/history
pages, opens the approvals drawer independently, and exposes location, screenshot, and sync state
through compact indicator popovers. It is **not** a consent surface:
it only reveals state and _reduces_ privilege, so — unlike `ConfirmDialog` — it needn't be a
top-layer `<dialog>`. The one authority moment (granting a capability) always stays in the
top-layer confirm. Consistent with invariant #4: a persistent panel is not a trust
indicator, and a spoofed decoy of it is inert (the real grant lives in shell `localStorage`,
and the browser's own site-settings revoke is the tamper-proof backstop).

## The bridge protocol (postMessage)

Display data and operator intent go straight to Haku's backend (it holds the `haku-state`
creds); the bridge carries only actions needing the trusted side. Every inbound message is
origin-checked (`event.origin === "https://haku-ui.allegedly.works"`) and schema-validated;
the iframe can only _request_ — the shell decides and acts. Wire shapes are defined once in
the shared `@haku/console-bridge` package (<../../js/bridge_protocol/protocol.ts>, owned here); the
shell's inbound validators and the open-link whitelist stay PR-gated in <../frontend/bridge.ts>.
Haku's UI will link the same package as a Bazel module from haku-state (migration tracked in
`haku/PLAN.md`).

### `routeChanged` / `titleChanged` — mirror browser chrome

haku-ui posts `{type: "routeChanged", path}` whenever its route changes. On the first such call,
the shared bridge client also starts a `MutationObserver` over the iframe document's `<head>` and
posts `{type: "titleChanged", title}` initially and whenever `document.title` changes. The shell
validates and mirrors the path into its own URL for refresh/deep-link restoration while Haku UI is
selected, remembering it while a `/_console/*` page is visible. The shell likewise copies each
bounded title only while Haku UI is selected; console-owned pages own their tab titles. The
cross-origin boundary prevents the shell from reading the iframe document directly.

### `requestLaunch` — fire the launch-routine capability

haku-ui renders its own launch dialog and posts `{type: "requestLaunch", id, prompt}`; the
shell pops its top-layer confirm showing the prompt **verbatim**, and only then fires with
the server-side bearer (see <../README.md> → _The capability tier_).

### Tool calls — routed through haku-ui backend, approved in trusted chrome

The `<tool-call>` affordance does **not** give the iframe a direct console bridge verb in v1.
haku-ui frontend posts to its own same-origin backend, that backend reads
`tool_requests/<id>.yaml` from haku-state and invokes haku-console's `/mcp` endpoint with its
configured static Agent token. If approval is
required, haku-console notifies its trusted frontend (`/api/events/ws`, with REST catch-up from
`/api/approvals/pending`) and renders the non-modal approvals panel itself. The iframe can request a
tool call, but cannot approve one or forge console chrome.

### `requestGeolocation` / `startGeolocationWatch` — read the operator's location (standing grant)

For a one-shot read, haku-ui posts `{type: "requestGeolocation", id, options?}` (the
`options` bag mirrors `getCurrentPosition`); for continuous tracking it posts
`{type: "startGeolocationWatch", id, options?}` (mirroring `watchPosition`) and later
`{type: "stopGeolocationWatch", id}`. Either way the shell replies with
`{type: "geolocationResult", id, ok, position?, code?, reason?}` — a plain, cloneable
`position` (flattened `GeolocationCoordinates` + `timestamp`) or a browser-shaped error
(`code` follows `GeolocationPositionError`: 1 `PERMISSION_DENIED`, 2 `POSITION_UNAVAILABLE`,
3 `TIMEOUT`). A one-shot gets one reply; a watch gets one reply **per fix**, same `id`, until
it ends. Why it must route through the shell, and how consent works:

- **The frame cannot read location itself.** The iframe has **no `allow="geolocation"`**,
  and the shell serves `Permissions-Policy: geolocation=(self)` — geolocation is delegated
  to nothing. Only the trusted top-level origin can read it, so the iframe must ask.
- **The shell holds every watch.** `startGeolocationWatch` runs the actual
  `navigator.geolocation.watchPosition` **in the shell** (`geolocation.ts` → `GeolocationWatcher`),
  keyed by the bridge `id`; each fix is relayed to the frame. So a prompt-injected Haku can
  neither start a watch silently (it needs the grant) nor keep one the operator has stopped
  (the shell owns `clearWatch`) — the Location panel's "Stop" is a real kill switch.
- **A shell-owned standing grant is the gate** (`geolocation_grant.ts`, persisted in the
  shell origin's `localStorage` — cross-origin isolated, so the frame can't read or forge
  it). The **first** ask with no grant pops the top-layer consent confirm ("Allow Haku to use
  your location?", which discloses continuous tracking); approving records the grant, so
  subsequent reads/watches start **without** re-confirming — "allow until withdrawn". The
  shell reads location _only_ while the grant is set.
- **Two independent gates.** The shell grant is the app-level gate; the browser's own
  geolocation permission (its native prompt on first read, its site-settings revoke) is the
  platform-level gate. Both must be "on" to read; withdrawing either stops reads.
- **Withdrawal** is a shell control in the Location panel: it
  stops every live watch and revokes the grant. Declining the confirm, withdrawing, or the
  watch ending yields `code: 1` (`PERMISSION_DENIED`; `reason` `declined`/`withdrawn`) so the
  frame treats it exactly like a native denial. The ⚙ indicator pulses while a watch streams.
- **Residual:** once granted, location (a live stream, under continuous tracking) is a datum
  the assumed-adversarial frame now holds and can exfiltrate only via the already-accepted
  browser channels (whitelisted `openLink`, WebRTC). Bounded by the operator's explicit
  grant, the shell-held watch, and the one-click stop; see `../../docs/security.md` →
  _Browser-side exfiltration_.

### `requestScreenshot` — capture the shell's own on-screen rect (standing grant)

haku-ui posts `{type: "requestScreenshot", id}`; the shell replies with
`{type: "screenshotResult", id, ok, imageDataUrl?, reason?}` — a PNG data URL cropped to the
iframe's live `getBoundingClientRect()`, or `ok:false` with a reason (`declined`, `withdrawn`,
or the browser's own picker error/dismissal). This is a real tab/window capture
(`getDisplayMedia`), not a DOM serialization — haku-ui's own `html-to-image` fallback
(`screenshot.ts`) exists precisely because a real capture can't be granted from inside the
sandboxed iframe. Why it must route through the shell, and how consent works:

- **The frame cannot capture the screen itself.** The iframe has **no
  `allow="display-capture"`**, and the shell serves `Permissions-Policy: display-capture=(self)`
  — capture is delegated to nothing. Only the trusted top-level origin can call
  `getDisplayMedia`, so the iframe must ask.
- **The shell holds the one live capture stream.** `screenshot_capture.ts`'s `ScreenshotSession`
  keeps the `getDisplayMedia` stream (decoded into a hidden `<video>`) alive across requests, so
  a prompt-injected Haku can neither start a capture silently (it needs the grant, and
  `getDisplayMedia` itself needs a genuine operator click) nor keep one running after the
  operator's own browser-native "Stop sharing" control or the Screenshot panel's withdraw ends it.
- **A shell-owned standing grant is the gate** (`screenshot_grant.ts`, persisted in the shell
  origin's `localStorage`, same shape as `geolocation_grant.ts` but its own key — cross-origin
  isolated, so the frame can't read or forge it). The first ask with no grant queues a
  non-modal approval card ("Allow screen capture for a screenshot?"); approving records the
  grant **and** opens the browser's own tab-share picker in the same click (the user gesture
  `getDisplayMedia` requires) — so subsequent requests, while the stream stays live, are instant
  frame grabs with **no** picker and no DOM walk.
- **Two gates, and an asymmetry from geolocation.** The shell grant is the app-level gate; the
  browser's own `getDisplayMedia` picker is the platform-level gate — but unlike geolocation's
  browser permission, the picker has **no persistent silent grant**: it reappears every time a
  _new_ stream is requested. So even with the standing grant already set, if the operator's
  browser-native "Stop sharing" ended the session, the next `requestScreenshot` still queues a
  fresh approval card (same title, "resume" in effect) to get a real click before reopening the
  picker — it cannot be served silently the way a granted geolocation read can.
- **Withdrawal** is a shell control in the Screenshot panel (the camera toggle's stop/withdraw
  panel): it stops the live stream (dropping the browser's own sharing indicator) and revokes
  the grant. Declining the approval, withdrawing, or the operator's own "Stop sharing" all leave
  the request unanswered only until the next ask; a request in flight at that moment resolves
  `ok:false` with `reason` `declined`/`withdrawn`.
- **Residual:** once granted, a captured screenshot is a datum the assumed-adversarial frame now
  holds and can exfiltrate only via the already-accepted browser channels (whitelisted
  `openLink`, WebRTC) — same bound as geolocation. Unlike geolocation there is no continuous
  stream handed to the frame itself; each capture is one bounded image the frame explicitly
  asked for. See `../../docs/security.md` → _Browser-side exfiltration_.

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
