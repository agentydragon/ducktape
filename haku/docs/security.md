# Haku — security model

The one document that states Haku's threat model, every enforcement mechanism, and the
invariants an editor must preserve. Everything here is enforced **outside the agent**;
Haku's instructions describe the perimeter, they never implement it. Detailed rationale
lives at the linked sources — this doc is the index and the contract, per the repo's
"deviations, not re-explanations" rule.

## Threat model

- **Haku is assumed adversarial.** Prompt injection through any readable source (an email
  body, a Tana note, a transaction memo, a Drive file) can invoke anything Haku can invoke.
  Consequently everything Haku authors inherits that trust level: the `haku-state` repo, the
  haku-ui backend, and — critically — **haku-ui frontend JS running in the operator's
  browser**.
- **Trusted:** the operator; reviewed ducktape code (the console, this base, the cluster
  manifests); the Kubernetes control plane and Authentik.
- **The operator's browser is a security-relevant execution surface**: it runs adversarial
  (Haku-authored) code with the operator's network vantage, fenced only by what the platform
  and operator-owned headers enforce (see _Browser-side exfiltration_).

## Doctrine

**The container is the trust boundary**: enforcement at
the credential / RBAC / network / proxy perimeter, never by in-agent permission rules — the
scanner runs `--dangerously-skip-permissions` _because_ the perimeter is what limits it.
Read-only by construction, in order of preference: (1) scope the upstream credential, (2)
front with a read-only MCP facade, (3) lock egress. Haku is **its own principal** everywhere
(attribution, independent revocation, bounded blast radius).

## Enforcement inventory

| #   | Mechanism                                                                                                                                                                                                            | What it bounds                                                                                                                                                                      | Source of truth                                                                                                            |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 1   | OIDC group `oidc-ksbx-groups:haku`                                                                                                                                                                                   | Haku's single cluster identity                                                                                                                                                      | `secrets/haku-k8s-jwt.yaml` + kube-apiserver `AuthenticationConfiguration`                                                 |
| 2   | `haku-sandbox-admin` Role — explicit resource allowlist, **no** `httproutes`/`gateways`                                                                                                                              | Full CRUD in `haku-sandbox` only; writes nowhere else                                                                                                                               | <../../cluster/k8s/haku/rbac/role.yaml>                                                                                    |
| 3   | `cluster-diagnostics-reader` + per-namespace `logs-configmaps-reader` bindings                                                                                                                                       | Cluster-wide read-only object/status; logs+configmaps only in infra namespaces (no secrets, no app-content namespaces)                                                              | <../../cluster/k8s/agents/agent-rbac-base/README.md>                                                                       |
| 4   | Kyverno `restrict-agent-gateway-routes`                                                                                                                                                                              | Agents cannot create public routes even if RBAC ever drifted                                                                                                                        | <../../cluster/k8s/kyverno/policies/restrict-agent-gateway-routes.yaml>                                                    |
| 5   | `haku-mitmproxy` egress fence on `haku-sandbox`                                                                                                                                                                      | All pod egress allowlisted at L3/L4 + L7                                                                                                                                            | <../../cluster/k8s/agents/haku-mitmproxy/>                                                                                 |
| 6   | Read-only credential set reflected into `haku-sandbox`                                                                                                                                                               | Every data source is read-only by construction (Plaid RO SQL user, all-`.readonly` Google token, `tana-mcp-ro` facade)                                                              | `haku/base/instructions.md` → _Setup: discover credentials_                                                                |
| 7   | `gmail-labeling` closure invariant                                                                                                                                                                                   | The **one sanctioned world-write**: labels under `haku/` only, enforced server-side before any Gmail call; the only `gmail.modify` credential stays server-side                     | <../gmail_labeling/SPEC.md>                                                                                                |
| 8   | base (image, read-only) vs. state (`haku-state`, Haku's only write)                                                                                                                                                  | Self-modification of Haku's contract is structurally a ducktape PR; operator base edits reach Haku via the `memory/base-sync.md` reconciliation pin                                 | <../base/README.md>                                                                                                        |
| 9   | Authentik proxy route to haku-ui, single-user policy binding, operator-owned                                                                                                                                         | Nobody but the signed-in operator reaches haku-ui, regardless of haku-state content — auth sits **in front of** the app, outside Haku's write scope                                 | <../../cluster/k8s/authentik/proxy-routes/haku-ui-httproute.yaml>, `cluster/k8s/authentik/app/blueprints/haku-ui-sso.yaml` |
| 10  | Console iframe containment: cross-origin isolation, `sandbox` without `allow-popups`/`allow-top-navigation`, console CSP `frame-src`, `openLink` scheme gate + operator-owned host whitelist + off-whitelist confirm | Agent UI cannot reach the launch bearer, open windows, or navigate the operator anywhere unvetted                                                                                   | <../console/docs/containment.md>                                                                                           |
| 11  | Capability tier: CSRF double-submit + server-side bearer + **top-layer `<dialog>` confirm**, audited in `haku-console` (a namespace Haku cannot read)                                                                | Privileged actions (`launch-routine`) require a genuine operator gesture on trusted chrome; agent UI can only _request_                                                             | <../console/README.md>                                                                                                     |
| 12  | CiliumNetworkPolicy: haku-ui ingress from authentik-server pods only                                                                                                                                                 | Forward-auth identity headers (`X-authentik-username`) can't be forged by direct in-cluster calls — operator intent written to haku-state is genuine                                | <../../cluster/k8s/haku/namespace/networkpolicy.yaml>                                                                      |
| 13  | Audit trails                                                                                                                                                                                                         | Every Haku proposal/decision is a git commit; LLM calls route through LiteLLM (attribution, budget, kill-switch) with Langfuse traces; capability invocations log in `haku-console` | `haku/base/instructions.md` (LiteLLM routing) + <../console/README.md> (capability audit)                                  |

## Browser-side exfiltration (channel inventory)

Adversarial haku-ui JS in the operator's browser holds personal data (haku-state content,
live source reads). Channels out, and what fences each:

| Channel                                                                              | Status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Navigate the visible window to `evil.example/?d=…`                                   | **Blocked structurally**: console CSP `frame-src` governs all nested-context navigations (incl. form submits); iframe sandbox has no `allow-popups`/`allow-top-navigation`; the only exit is `openLink`, which is scheme-gated and whitelist/confirm-gated. **This is why the console must stay the top-level window** — a top-level haku-ui could always navigate itself to a data-bearing third-party URL; no web-platform mechanism blocks that from outside the app (CSP `navigate-to` never shipped).                                                                              |
| Silent subresource beacons (`fetch`/`<img>`/`sendBeacon`/WebSocket) to a third party | **Blocked by the operator-injected CSP** on the haku-ui `HTTPRoute` (`ResponseHeaderModifier` sets `connect-src 'self'`, `img-src 'self' data:`, … — the same mechanism <../../cluster/k8s/authentik/app/httproute.yaml> uses). Subresource loads obey the _document's own_ CSP, which Haku serves — so the fence must be injected at the route, the only public door, where `set` overrides anything Haku's backend sends. The `webrtc 'block'` directive additionally closes the WebRTC data-channel bypass of `connect-src` in Chromium (CSP3; Firefox/Safari ignore it harmlessly). |
| haku-ui backend egress                                                               | Fenced by #5 (mitmproxy allowlist).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Data in URLs to **whitelisted** hosts (`claude.ai`, `github.com`, …) via `openLink`  | **Accepted residual** — the handoff use-case requires it; whitelist hosts are semi-trusted.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Consent-confirmed navigations                                                        | Working as designed — the confirm shows the full URL; the operator is the gate.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Covert channels (timing, DNS tricks)                                                 | Out of scope; the goal is closing practical bulk channels.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

## Invariants — preserve these when changing anything

1. Never add `httproutes`/`gateways` (or `networkpolicies`) to Haku's Role; never weaken the
   Kyverno route denylist.
2. Every credential reflected into `haku-sandbox` is read-only/scoped; a write-capable
   credential requires its own closure-style server (per #7) and a base update naming it.
3. The console renders **no** Haku-authored content and holds no haku-state credential; the
   litmus test for console code: _does it hold a secret, perform a privileged action, or
   define the trust boundary?_ If not, it belongs to Haku.
4. Consent moments happen on trusted top-level/top-layer surfaces only (the capability
   confirm, the `openLink` off-whitelist confirm). Never move a confirm into agent-embeddable
   chrome; a persistent "trust badge" is not a control.
5. The `openLink` host whitelist lives in the shell (ducktape), never in haku-state.
6. **The console stays the outer window.** Do not make haku-ui top-level: exfiltration
   containment (navigation channel above) depends on the embedding. Evaluated and rejected
   2026-07-01 (haku-state `plans/url-topology.md`).
7. Auth for haku-ui sits in operator-owned Authentik/route objects (#9) — never in
   haku-state-served code.
8. If haku-state ever vendors third-party content, re-examine every "haku-state is
   single-author, so rendering it live is safe" assumption (garden MDX eval).

## Known gaps (tracked)

- **Verify the injected haku-ui CSP after each deploy-affecting change** — confirm the
  header lands on live `haku-ui.allegedly.works` responses and the SPA + in-iframe SSO
  still work (Haku item `haku-ui-browser-exfil-csp-gap-2026` tracks the initial
  verification).
- **Gateway `allowedRoutes` still `from: All`** — belt-and-suspenders tightening deferred,
  fenced meanwhile by Kyverno (#4); see `cluster/k8s/TODO.md`.
- **Tool inputs/outputs flow to the model provider's control plane** regardless of sandbox
  location — inherent to using hosted models; acknowledged in
  <../runtime/managed_agent/anthropic_hosted/README.md>.
