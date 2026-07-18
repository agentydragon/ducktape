# ibkr_mcp

Read-only Interactive Brokers **market-data** MCP server. Its tool surface is
reflected from IBKR's own Client Portal Web API spec via FastMCP's
`OpenAPIProvider` — the same pattern as <../grocy_mcp/> — filtered to a
read-only allowlist and transcoded to OpenAPI 3.1 at build time. It fronts a
co-located Client Portal Gateway that holds a single authenticated **paper**
session, so quotes flow but no order can ever be placed.

## Why a paper login / read-only

- IBKR session collisions are per-**username**, so the gateway uses a dedicated
  login (a paper-trading username) that never fights with the account holder's
  live TWS/mobile sessions.
- A paper account gets real (delayed, free-tier) quotes on real symbols but
  **cannot place a live trade** — safety by construction.
- Two independent guards keep it read-only: the paper login (cannot place a live
  trade at all) and this server's allowlist (no order route is ever a tool). The
  gateway socket stays pod-local, never exposed. (The TWS socket API's "Read-Only
  API" toggle has no equivalent on this Web API / IBeam path, so it is not one of
  the guards here.)

## The read-only allowlist — the safety core

IBKR's spec ships order-placement routes next to the market-data routes. Only
the operations in <route_policy.py> ever become tools; everything else is absent
by construction. Two guards, one test:

1. **Build time** — <spec_fixup.py> (the `ibkr_openapi_fixed` genrule) emits an
   OpenAPI document containing **only** the allowlisted operations. It raises if
   an allowlisted path is missing upstream, so `allowlist ⊆ IBKR's real spec` is
   enforced by the build.
2. **Runtime** — `server._customize_component` raises if asked to surface an
   operation not on the allowlist.
3. **Test** — `test_route_policy` asserts no trading/account route is on the
   allowlist and that the generated spec contains exactly the allowlist.

### Tool surface

Market data / lookups: `market_data_snapshot`, `market_data_history`,
`secdef_search`, `secdef_info`, `secdef_strikes`, `contract_info`,
`scanner_params`, `scanner_run`.

Session lifecycle (thin wrappers reflected from the same spec): `session_status`
(`/iserver/auth/status`) and `request_reauth` (`/iserver/reauthenticate`).
Keeping the session alive (`/tickle`) is IBeam's job, not a tool.

Tools are faithful to IBKR's API: `market_data_snapshot` takes `conids`
(resolve a symbol first with `secdef_search`) and numeric `fields` codes, and
returns delayed values on the free tier.

## Architecture

```text
              cluster-internal only                      OAuth-gated front door
  ┌──────────────────────────────────────┐          ┌──────────────────────────────┐
  │  ibkr-mcp pod (namespace ibkr)        │          │  Haku  ──▶  haku-console      │
  │  ┌──────────────┐  localhost:5000     │          │            (operator OAuth)   │
  │  │ IBeam / CP   │◀───────────┐        │          └───────────────┬──────────────┘
  │  │ Gateway      │            │        │   HTTPRoute               │
  │  │  (paper)     │   ┌────────▼──────┐ │  ibkr-mcp.allegedly.works ▼
  │  └──────┬───────┘   │ ibkr_mcp      │◀┼───────────────► OIDCProxy (agentydragon)
  │  IBKR 2FA login     │ OpenAPIProvider│ │          also reachable by claude.ai users
  │  (paper username)   │ READ-ONLY      │ │
  └─────────────────────┴────────────────┴─┘
```

`server.py` binds one long-lived `httpx.AsyncClient` (its cookie jar carries the
gateway session) to every generated tool. There is no per-caller identity — the
gateway **is** the identity. Front-door auth is the shared Authentik
`build_authentik_auth` (`mcp_infra/authentik_auth`), an `OIDCProxy` restricted to
the **agentydragon** user. **Haku reaches it through haku-console** (operator
OAuth — the console registers via DCR as the approving operator), the same front
door claude.ai uses; there is **no** machine token / `direct_jwt_trust`. The
console entry's `id` is `interactive_brokers`, so tools surface to Haku prefixed
as `interactive_brokers_secdef_search`,
`interactive_brokers_market_data_snapshot`, etc., through the console's approval
queue. (The deployed host/namespace keep the shorter `ibkr-mcp` name.)

## Free-tier data + the weekly re-auth

Free-tier IBKR data is **delayed** (~15 min). US-equity delayed data is
restricted for some IB entities — the go/no-go check (below) confirms coverage
for the instruments you care about.

The gateway session survives IBKR's daily restart automatically but expires
weekly (~Sun 01:00 ET), needing a fresh login with a phone 2FA tap. That flow is
first-class here: Haku calls `session_status`, and on `not authenticated` calls
`request_reauth` (which fires the IBKR Mobile push), surfaces a "tap to re-auth"
nudge, then polls `session_status` until authenticated. The tap itself is
out-of-band — no server can press it.

## The OpenAPI spec

Pinned in <../MODULE.bazel> as the `ibkr_cpapi_openapi_spec` `http_file` (a
Swagger 2.0 mirror of IBKR's Client Portal Web API). The authoritative spec is
whatever the running gateway serves; refresh by dumping the gateway's own spec,
re-pinning the URL + sha256, and re-running the tests — `spec_fixup` re-filters
and re-transcodes it.

## Deploying

- **Image** — `//ibkr_mcp:server_image` → `git.allegedly.works/ducktape-ci/ibkr-mcp`
  (private Forgejo registry; `registry: forgejo` in `push-images.yml`). Flux image
  automation (<../cluster/k8s/flux-image-automation-forgejo/>) rolls new `devel-*`
  tags out. The pod pulls with a `forgejo-images-creds` dockerconfigjson that ESO
  mirrors into the `ibkr` namespace from flux-system
  (<../cluster/k8s/ibkr/forgejo-images-creds-eso.yaml>; there's a TODO there to
  reconsider the reflector once its sops allowlist can be edited).
- **k8s / Flux** — <../cluster/k8s/ibkr/>: the two-container pod (IBeam gateway +
  this server), Service, HTTPRoute, a CNPG Postgres cluster (OAuth state), and the
  `ibkr-paper-trading-credentials` Secret, applied by the `ibkr-mcp` Flux
  Kustomization.
- **Terraform** — <../tf/gitops/agent-machine-access/ibkr-mcp.tf>: the Authentik
  OAuth2 provider/application restricted to agentydragon, and the `ibkr-mcp-oidc`
  Secret the pod reads.
- **Haku** — the `interactive_brokers` entry in <../cluster/k8s/haku/console/config.yaml>.

## Status

Built and tested, but operationally suspended as of 2026-07-17 because the paper
account application is still in progress. The `ibkr-mcp` Flux Kustomization and
image scan are suspended, the live workload/database resources were removed,
and the server is not registered in Haku Console. The namespace and Authentik
plumbing remain dormant so the shared Terraform stack can continue reconciling.

See <TODO.md> for the dated account-status check and reactivation checklist. The
service must complete its first paper login and the **live free-tier data
verification** before it is declared live. IBeam remains pinned to `:latest`
until that first successful run, then should be pinned to its digest.
