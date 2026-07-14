# OAuth and identity architecture across Ducktape

- **Status:** approved architecture; migration in progress
- **Written:** 2026-07-13
- **Repository baseline:** `origin/devel` at `1c1b109cab`; includes the verified-principal hardening in PR #3188 and the Authentik module split in PR #3189
- **Haku prototype reviewed:** PR [#3122](https://github.com/agentydragon/ducktape/pull/3122), closed as superseded; squashed commit `3e8f7a311` remains source material
- **Approved implementation completed:** FastMCP `3.4.4`, the latest stable release verified on 2026-07-13, is pinned on `devel`
- **P2 research artifact:** closed non-merge PR [#3191](https://github.com/agentydragon/ducktape/pull/3191), commit [`f8054ec35`](https://github.com/agentydragon/ducktape/commit/f8054ec35); its branch preserves the executable spike, while only the durable decision is recorded here
- **Scope:** MCP authorization, Haku Console agent identity, browser OIDC, Authentik, OAuth-backed applications, and machine credentials in this repository

## Decision

Default to FastMCP as the OAuth authorization-server proxy and resource-server engine for FastMCP-based services that need a local MCP-compatible authorization-server facade. Preserve direct Authentik JWT validation or `RemoteAuthProvider` for servers that only need to accept tokens from an external issuer. Do not replace FastMCP's proxy with a home-grown FastAPI/Authlib authorization server unless the adapter containment gate fails and the evaluated existing-AS fallbacks also fail; do not centralize every OAuth role into a new Ducktape auth service.

The long-term boundary should be:

- Authentik authenticates people and machines, applies IdP policy, and issues or exchanges upstream credentials.
- FastMCP implements the MCP-facing OAuth protocol: discovery, client metadata, authorization transactions, PKCE, redirect validation, codes, local access and refresh tokens, token swap, and MCP bearer verification.
- FastAPI and Starlette compose the web application, routes, dependencies, sessions, CSRF protection, and Jinja templates.
- Haku owns the `Operator`, `Agent`, agent-enrollment ceremony, agent names, ownership, lifecycle, Agent-facing MCP hub, downstream-server composition, approval policy, UI, and audit semantics.
- Haku is multi-Operator and multi-Agent: an Agent acts on behalf of exactly one owning Operator, Agent reads never widen to sibling Agents, and any non-auto-approved call is visible and decidable only by that Operator.
- Airlock owns its current live hub/policy and external-provider credential broker while their consumers remain. It is not a required target-state boundary for Haku: Haku Console's only direct Airlock dependency is currently the singleton Google grant that Airlock provisions and refreshes.
- Shared Ducktape packages expose narrow, typed protocol building blocks. They must not know Haku's Agent model or Airlock's business policy.

The branch-only P2 off-production spike in closed non-merge PR [#3191](https://github.com/agentydragon/ducktape/pull/3191) showed that Haku can compose the enrollment ceremony around FastMCP's public authorization path without patching or replacing its callback, transaction store, redirect validation, PKCE handling, or code/token issuance. A Haku-local adapter calls public `OAuthProxy.authorize()` with `AuthorizationParams`, temporally reserves the exact validated `(client_id, redirect_uri, S256 code_challenge)` tuple from those public inputs only after `authorize()` succeeds, and sends the browser through a Haku-owned enrollment interaction before following FastMCP's opaque upstream authorization URL. At downstream code exchange, public `AuthorizationCode` data provides the same tuple; P1's resolver verifies the MCP-side OIDC principal, and P3's canonical identity mapping resolves that principal to the Operator used for the equality check.

The sole accepted private compatibility seam remains P1's read/delete access to FastMCP's `_code_store`. The branch-only P2 adapter did not parse FastMCP state, access `_transaction_store`, intercept or rewrite the IdP callback, copy token issuance, or override route construction. It version-pinned two protected compatibility hooks, `_extract_upstream_claims` and `_translate_scopes_from_idp`, to carry only opaque `grant_id` context and prevent scope broadening. A future public authorization-code inspection/consumption API would delete the remaining private seam; public typed token-context and scope-preservation hooks would delete the protected overrides. Ducktape will not currently make an upstream FastMCP contribution or wait for one.

FastMCP's current consent point remains pre-IdP. The P2 spike used that ordering deliberately: the Haku page authenticates its browser through Haku's own Operator session, collects the Agent name and explicit consent, and reserves the decision before forwarding the browser to Authentik. No Agent is created at that point. After FastMCP's untouched callback creates the downstream authorization code, token exchange resolves the verified MCP principal and requires its canonical Operator UUID to equal the browser session's canonical Operator UUID before creating the Agent and issuing grant.

At the research baseline, `ResilientOIDCProxy` had legitimate reasons to exist, but its name and scope were too broad: it combined three unrelated compatibility repairs. They are now isolated and named after the behavior they repair so each can be tested and removed independently when its consumers no longer need it or FastMCP supplies the behavior.

FastMCP `3.4.4` is now the repository baseline. PR [#3146](https://github.com/agentydragon/ducktape/pull/3146) updated the Python/Bazel lock, the `fastmcp-slim` package split, and the Nix packages together; its focused OAuth/Haku suite, Nix closures, CLI smoke tests, all-files pre-commit, and GitHub CI passed. Recheck the latest stable release before every future repin rather than treating `3.4.4` as a permanent ceiling. This baseline includes FastMCP PR [#3960](https://github.com/PrefectHQ/fastmcp/pull/3960), which changed `require_authorization_consent=True` to always show consent after a confused-deputy vulnerability in remembered consent. Ducktape pins that behavior with the repeated-authorization integration test landed in PR #3154.

FastMCP encrypts its default file store, but in both `3.2.4` and `3.4.4` it uses a supplied `client_storage` exactly as passed. Ducktape therefore serializes FastMCP state as ordinary JSONB or Valkey values. This plan accepts the repo's Postgres/Valkey services as a private trusted storage boundary. Application-level encryption is optional defense-in-depth, not a prerequisite for the architecture work.

## Non-goals

This plan does not:

- make Authentik the database for Haku agents;
- treat OAuth consent, browser login, agent enrollment, and downstream-provider connection as one generic flow;
- require every MCP server to use `OIDCProxy`;
- require a standalone identity microservice;
- depend on an unreleased Authentik DCR implementation or MCP SDK v2;
- retire Airlock, migrate its remaining non-Haku consumers, or revive/delete experimental `<../x/agent_server/>` code during the near-term Haku OAuth work;
- use an OAuth `client_id`, an access-token JTI, a username, or a display name as Haku's canonical agent identity; or
- promise that an MCP client will notify Haku when a user removes or forgets a connector.

## The terms that must not collapse together

Most of the current architectural friction comes from one identifier being asked to mean several different things. These entities have different owners, cardinalities, and lifecycles.

| Entity                      | What it identifies                                              | Owner                                             | Stable for                                                     | Must not be used as                               |
| --------------------------- | --------------------------------------------------------------- | ------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------- |
| OAuth client software       | Claude.ai, Claude Code, Codex, or another client implementation | Client author / authorization server registration | A client registration or published Client ID Metadata Document | A Haku agent installation                         |
| OAuth `client_id`           | The registered client software metadata                         | Authorization server                              | DCR record, preregistration, or CIMD document                  | A person, grant, device, or Haku `agent_id`       |
| Resource owner              | The person authorizing access                                   | Upstream IdP                                      | An issuer-scoped subject                                       | A mutable username                                |
| OIDC identity               | `(issuer, subject)`                                             | Authentik and the application linking it          | An IdP identity                                                | A bare `sub` assumed global across issuers        |
| Haku Operator               | A local Haku account/owner                                      | Haku                                              | Product lifetime                                               | An Authentik provider slug                        |
| Haku Agent                  | A named actor the operator recognizes                           | Haku                                              | Until explicitly disabled/deleted                              | A token, DCR row, or display string               |
| OAuth agent grant           | One authorization relationship between a client and an Agent    | Haku plus the MCP authorization server            | One token family / reconnect lifecycle                         | The Agent itself                                  |
| Access token                | A bearer credential for a resource and scopes                   | Authorization server                              | Minutes or hours                                               | Durable identity                                  |
| Refresh token               | A credential that rotates a token family                        | Authorization server                              | Until expiry/revocation/rotation                               | Durable identity                                  |
| Browser session             | A browser's logged-in application session                       | Haku/Props/etc.                                   | Session lifetime                                               | An MCP credential                                 |
| Authorization transaction   | One in-progress `/authorize` attempt                            | FastMCP                                           | Minutes                                                        | A client registration or Agent                    |
| Consent interaction binding | Browser/form binding and CSRF state for one interaction         | FastMCP built-in consent or the Haku adapter      | Minutes; one-time for Haku enrollment                          | Proof of operator identity or an Agent credential |

The current MCP authorization specification makes this distinction especially important. Its preferred order is preregistration, then a Client ID Metadata Document, then DCR as a fallback. A CIMD `client_id` is an HTTPS URL naming shared client software. Multiple people and installations can therefore present the same `client_id`. DCR is explicitly a machine-to-machine registration operation without a resource owner. Agent naming cannot live at `/register`, and `client_id` cannot be the Agent primary key. See the [MCP 2025-11-25 authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

The intended product experience can still be described as “register this Agent.” A new MCP client dynamically registers or otherwise presents its client metadata, then opens the browser authorization flow. Haku authenticates its own browser session through Authentik, shows the requesting client and scopes, and asks: authorize this new Agent to act as this Operator, and what should it be called? Acceptance reserves the name and continues the MCP-side Authentik flow; only verified token exchange with the same canonical Operator creates the Haku Agent plus its credential binding/grant. This does not turn the unauthenticated DCR endpoint itself into a form or identity boundary.

## Current Ducktape topology

Ducktape does not have one OAuth problem. It currently has at least seven different roles.

### MCP authorization server and resource server

`<../mcp_infra/authentik_auth/provider.py>` builds a FastMCP `OIDCProxy` and optional direct JWT verifiers under `MultiAuth`. Consumers include:

- the generic credentialed facades in `<../mcp_infra/oauth_facade/>`;
- the identity-preserving Grocy servers in `<../grocy_mcp/>`;
- Haku Console's agent-facing `/mcp` endpoint in `<../haku/console/mcp_server.py>`; and
- Airlock's MCP endpoint in `<../airlock/app.py>`.

This legitimate shared layer is split by responsibility:

- `<../mcp_infra/authentik_auth/config.py>` owns Authentik URL and configuration conventions;
- `<../mcp_infra/authentik_auth/fastmcp_proxy.py>` owns the narrow FastMCP refresh and downstream-identity compatibility behavior;
- `<../mcp_infra/authentik_auth/provider.py>` owns provider construction and composition;
- `<../mcp_infra/authentik_auth/token_exchange.py>` owns request-scoped backend token exchange; and
- `<../mcp_infra/authentik_auth/oidc_principal.py>` owns verified upstream-principal resolution.

Those are related, but not one abstraction.

### Generic credentialed MCP facades

`<../mcp_infra/oauth_facade/>` fronts an upstream HTTP or stdio MCP server with an Authentik gate. Tana read-write, Manifold, PostScan Mail, and Plaid DB deployments use this general shape. The upstream credential is server-held, so the incoming human identity controls admission but is intentionally erased before the backend hop.

That behavior is valid, but it should be named honestly: it is a **credentialed facade**, not identity delegation. The authorization question is “may this operator reach this shared backend credential?”

### Identity-preserving delegation

`<../grocy_mcp/server.py>` and `AuthentikTokenExchanger` implement a different pattern. The incoming, verified Authentik user token is exchanged via an Authentik JWT-bearer assertion for a token scoped to a proxy provider. The backend outpost sees the same user and injects trusted identity headers.

That is a **delegating token exchange**, not the generic facade pattern. It should remain distinct in names, types, tests, and threat model.

### Haku Console's three OAuth roles

Haku is simultaneously:

1. a browser OIDC relying party for the operator, using Authlib and a signed Starlette session in `<../haku/console/operator_auth.py>`;
2. an MCP resource server plus local OAuth proxy, downstream-server hub, and approval/audit control plane for agents, using FastMCP in `<../haku/console/mcp_server.py>`; and
3. an OAuth client connecting the operator to downstream MCP servers, with durable flow/token state in `<../haku/console/mcp_operator_oauth.py>`.

These roles share an operator domain, but they are different protocol directions. Combining them into one “Haku OAuth” class would make the boundaries worse.

### Airlock's current four auth roles

Airlock currently combines:

1. an MCP resource server and OAuth proxy;
2. a browser SPA protected by direct Authentik JWTs;
3. a credential broker for Oura, Google, and BSC grants; and
4. a target of machine credentials from OpenClaw and other proposers.

The external-provider broker in `<../airlock/oauth/>` is not MCP authorization. It authorizes Airlock to hold shared downstream API credentials and publish them into Kubernetes Secrets. Its policy and lifecycle currently belong to Airlock even if Authlib supplies the HTTP protocol mechanics. The current proxy-enabled deployment does not correctly compose all intended direct-JWT contracts; the focused audit below treats that as wiring to repair, not proof those paths currently work.

For Haku Console, that role is already much narrower. Airlock performs Google consent and refresh for the singleton `haku_console_google` grant and writes `haku-console-google-access-token`; External Secrets Operator mirrors only that access token into the `haku-console` namespace. Haku Console consumes it for its in-process Gmail and Google Calendar servers, while Haku already owns proposal, policy, operator decision, execution, result, and audit. Airlock is not on Haku's tool-call authorization path.

Do not deepen that dependency. In the eventual multi-Operator design, Google access should be a Haku-owned, per-Operator downstream-provider connection selected at execution time, separate from both MCP Agent enrollment and the Agent's OAuth grant. Haku's private application storage may hold its refresh/token state; the current singleton Kubernetes Secret is not the final multi-Operator identity model. Airlock still has independent OpenClaw, Claude Code, provider, and backend consumers, so retiring it requires a separate inventory and migration rather than being coupled to Haku's OAuth work.

### Historical prior art in `x/agent_server`

`<../x/agent_server/>` previously explored a FastAPI Agent runtime with one global user-facing MCP compositor, dynamically mounted per-Agent compositors, Agent lifecycle tools, and a pre-dispatch approval-policy gateway. Its useful ideas are one policy front door, explicit per-Agent isolation, a distinct human-approver boundary, and visible Agent lifecycle.

Treat it as design archaeology, not an implementation base. Code under `x/` is explicitly experimental, and this version used static string-token routing, a globally privileged user surface, blocking approval, dynamic-compositor coupling, and a single-user runtime model. Do not revive it, depend on it, or delete it as part of this plan. Haku's canonical Operator/Agent/grant/binding model and durable promise/audit semantics remain authoritative.

### Ordinary browser OIDC relying parties

Haku and Props use Authlib's Starlette client integration. Study Casino hand-rolls discovery, state, token exchange, and session logic in `<../x/study_casino/auth.py>` and uses `preferred_username` as durable application identity. These applications need a small shared relying-party helper, not an authorization-server framework.

### Direct Authentik OAuth for native servers

`kubectl-passthrough-mcp` and `kubectl-sandbox-mcp` use the upstream Kubernetes MCP server with preregistered public clients and Authentik-issued JWTs. Authentik scopes the identity at token issue; the MCP server validates/passes through that token. These do not need FastMCP `OIDCProxy` because Authentik can directly issue a token acceptable to the resource server.

`<../cluster/docs/mcp_oauth_authentik_notes.md>` still describes DCR as the default MCP expectation. The current MCP specification prefers preregistration/CIMD and treats DCR as fallback, so that document needs a standards update without changing the deployed preregistration design.

### Reverse-proxy SSO and machine credentials

Authentik proxy outposts, oauth2-proxy deployments, client-credentials rotators, direct long-lived JWTs, static bearer tokens, and JWT-bearer exchanges are additional distinct modes. They are not interchangeable just because all eventually put a bearer in an HTTP header.

## Responsibility map

| Component                  | It should own                                                                                                                              | It should not own                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| MCP specification          | Client/resource-server behavior, discovery, registration mechanisms, PKCE, resource indicators, bearer usage                               | Ducktape's Agent model or an authorization-server implementation             |
| MCP Python SDK             | OAuth provider protocol, standard authorization/token/register/revoke route handlers and models                                            | Haku UI and domain policy                                                    |
| FastMCP                    | MCP AS/RS composition, OAuth proxy state machine, storage, local tokens, token swap, `MultiAuth`, authorization checks                     | Product-specific agent records or templates                                  |
| FastAPI                    | Application routing, dependencies, exception handling, OpenAPI, application composition                                                    | OAuth protocol correctness by itself                                         |
| Starlette                  | ASGI routing/mounts/lifespan, middleware, sessions, requests/responses, Jinja integration                                                  | Durable identity, token storage, or authorization policy                     |
| Authlib client integration | Browser RP discovery, state/nonce, authorization-code exchange, token parsing                                                              | MCP authorization-server routes                                              |
| Authlib OAuth server core  | A useful model for separating request validation, user interaction, and response completion                                                | A ready-made FastAPI MCP authorization server; Authlib ships no such adapter |
| Authentik                  | Human and service authentication, IdP sessions, provider access policies, claims/scopes, token issuance, logout/revocation where supported | Haku agent naming, Haku grant records, or Airlock's product policy           |
| `mcp_infra`                | Typed FastMCP/Authentik composition, token verification/exchange, narrow compatibility shims                                               | Haku/Airlock domain state or HTML                                            |
| Haku                       | Operator/Agent identity, enrollment, Agent-facing hub, downstream composition, approval policy, ownership, names, revoke/disable UI, audit | Reimplementing OAuth redirects, PKCE, codes, or token rotation               |
| Airlock                    | Current non-Haku hub/policy consumers and external-provider credential provisioning until migrated                                         | Haku's target-state hub or a second hand-written generic OAuth library       |

The MCP specification explicitly says the MCP server is an OAuth resource server and that authorization-server implementation details are out of scope. FastMCP fills that implementation gap. FastAPI's security helpers validate credentials at application endpoints; they do not create an OAuth authorization server. Starlette supplies public composition seams such as `AuthenticationMiddleware`, `SessionMiddleware`, and `Jinja2Templates`.

Authlib's authorization-server examples show the missing shape clearly: validate the protocol request, obtain a consent grant, let the application authenticate/render/decide, then complete the authorization response. We want that separation inside FastMCP rather than a second implementation beside it. See [Authlib's authorization-server documentation](https://docs.authlib.org/en/latest/flask/2/authorization-server.html).

## The target Haku flow

There are two nested OAuth flows, a browser, and one Haku ceremony, not one flow. The MCP client launches the browser but does not receive or authenticate with the browser's cookies.

```text
MCP client            Browser               Haku/FastMCP             Authentik          Haku domain
    |                    |                         |                       |                   |
    | discover/register  |                         |                       |                   |
    |--------------------------------------------->|                       |                   |
    | open /authorize + state + PKCE               |                       |                   |
    |------------------->|------------------------>| validate + create transaction T             |
    |                    |                         | reserve tuple; create I(AwaitingBrowser) -->|
    |                    |<------------------------| redirect to Haku enrollment                 |
    |                    |------------------------>| Haku login/session; resolve Operator ------>|
    |                    |                         | I -> AwaitingApproval; bind identity ------>|
    |                    |<------------------------| render enrollment for I and Operator       |
    |                    | name/create/reconnect   |                       |                   |
    |                    |------------------------>| CSRF + Origin + one-time I; reserve name -->|
    |                    |<------------------------| redirect to opaque FastMCP upstream URL     |
    |                    |------------------------------------------------>| authenticate       |
    |                    |<------------------------------------------------| callback            |
    |                    |------------------------>| untouched FastMCP callback creates code    |
    |                    |<------------------------| redirect with downstream code              |
    |<-------------------|                         |                       |                   |
    | POST /token + verifier                       |                       |                   |
    |--------------------------------------------->| read public code tuple; verify MCP principal|
    |                                              | require same canonical Operator ---------->|
    |                                              | create Agent + issuing grant G ----------->|
    |                                              | public issuance persists family with G     |
    | Bearer token carrying/resolving only G       |                       |                   |
    |<---------------------------------------------|                       |                   |
    | MCP call                                     |                       |                   |
    |--------------------------------------------->| verify bearer and resolve G                |
    |                                              | first-use activate/enforce -------------->|
```

The state domains in that diagram must stay separate:

- the MCP client's outer OAuth `state` and PKCE verifier;
- FastMCP's opaque upstream OAuth URL and state, which Haku neither parses nor rewrites;
- the local `EnrollmentInteraction`, exact-tuple temporal reservation, one-time browser nonce, CSRF token, and Origin check; and
- the Haku Operator browser session, authenticated independently of the MCP-side IdP flow.

They may be correlated through opaque server-side identifiers, but one must not substitute for another.

The exact public `(client_id, redirect_uri, S256 code_challenge)` tuple is only a temporal collision key. At most one live interaction may reserve it. When an interaction closes, its digest remains tombstoned for strictly longer than FastMCP's maximum transaction lifetime plus its authorization-code lifetime, including a pinned safety margin. This prevents a late code from an older authorization binding to a newer interaction that reused the tuple. The timeout is a version-pinned compatibility fact and must be rechecked on every FastMCP upgrade; the tuple never becomes proof of browser identity or Operator authority.

## FastMCP's intended extension ladder

Use the least powerful layer that fits each server.

### 1. `TokenVerifier`

Use a verifier when the server only needs to accept already-issued tokens. `JWTVerifier` is appropriate when Authentik directly issues a JWT with the correct issuer, audience, and scopes.

Examples: native Kubernetes MCP servers and direct machine JWT paths.

### 2. `RemoteAuthProvider`

Use `RemoteAuthProvider` when the MCP resource server validates tokens from a known external authorization server and needs to advertise that server through protected-resource metadata. This is a cleaner fit than `OIDCProxy` when no DCR/CIMD facade, local token family, or upstream-token translation is required.

### 3. `OAuthProxy` / `OIDCProxy`

Use `OIDCProxy` when the upstream provider is OIDC and the MCP client needs an MCP-compatible local authorization-server facade: DCR/CIMD, redirect validation, local codes/tokens, or token translation. This is the right layer for the generic facades, Grocy, and Haku's interactive MCP clients.

### 4. `MultiAuth`

Use `MultiAuth` to compose one route-owning interactive authorization server with explicit additional token verifiers. Each verifier must represent one coherent trust contract. Do not build issuer/audience cross-products from independently merged lists.

Examples: Haku interactive agents plus static agents; Airlock interactive MCP clients plus the exact direct machine/browser JWT issuers it intentionally accepts.

FastMCP `3.4.4` treats every verifier exception as a non-match inside `MultiAuth`. Use stock `MultiAuth` only where that failure policy is acceptable. Haku's grant/store-aware provider needs an adapter-owned composite that continues on a clean `None` but preserves classified operational failures such as a database or OAuth-state-store outage.

### 5. `OAuthProvider`

Implement the full public MCP SDK provider protocol only if Ducktape truly needs a custom authorization server. This means owning client lookup/registration, authorization, code exchange, token refresh, token loading, and revocation. It is a substantial security surface and is not justified merely to change a consent page.

### 6. ASGI composition

Mount `mcp.http_app()` as a Starlette subapplication. Parent FastAPI dependencies apply only to parent-owned `APIRouter` routes; they do not secure the mounted FastMCP app. Preserve the FastMCP lifespan, let its auth provider secure `/mcp`, and expose root well-known routes explicitly when mounting under a prefix.

FastMCP `custom_route` is not wrapped by FastMCP's `RequireAuthMiddleware`. Its auth-context middleware may parse a supplied bearer, but absence or invalidity is not rejected. It is appropriate for health/readiness, not Haku's authenticated agent-management UI. Use an owning FastAPI router with explicit dependencies instead.

## What is public, protected-in-practice, and private

For the pinned FastMCP `3.4.4`, treat these boundaries differently. References to `3.2.4` below are historical comparisons used to explain why the compatibility code exists, not a supported second runtime.

### Public and intended

- `AuthProvider`, `TokenVerifier`, `RemoteAuthProvider`, `OAuthProvider`, and `MultiAuth`;
- `OAuthProxy` and `OIDCProxy` constructor configuration;
- `get_routes()`, `get_well_known_routes()`, and `get_middleware()`;
- FastAPI/Starlette mounting with the FastMCP lifespan;
- `AuthCheck` and scope/tag authorization at tools/resources/prompts;
- `OIDCProxy` token-verifier injection;
- public `OAuthProxy` scope configuration, including constructor `valid_scopes` where exposed and `update_default_scopes()` on an already-built `OIDCProxy`; and
- standard provider protocol methods if implementing a complete `OAuthProvider`.

### Protected-in-practice, not compatibility-promised

FastMCP's provider subclasses override methods such as `_create_upstream_oauth_client`, scope translation hooks, and `_extract_upstream_claims`. A Ducktape subclass can use one of these only when:

- the override is isolated in a compatibility module;
- the exact upstream version is pinned;
- a contract test demonstrates why it exists; and
- an existing upstream tracking reference, when available, plus a local rationale and deletion condition are recorded.

The leading underscore still means upstream may change it without compatibility guarantees.

### Unequivocally private

- `_transaction_store`;
- `_code_store`;
- `_client_storage`, `_client_store`, and their collection/model layouts;
- `_show_consent_page()` and `_submit_consent()`;
- consent cookie names and encoding helpers; and
- rewriting the body of FastMCP's generated response.

Haku currently needs behavior that is only reachable through this private group. That is a framework gap, not a reason to pretend the APIs are stable.

FastMCP issue [#4299](https://github.com/PrefectHQ/fastmcp/issues/4299) requests a custom consent renderer and remains open with no milestone or implementation. A renderer alone is insufficient for Haku because Haku also needs validated custom form data, verified upstream identity, and grant-scoped claims/lifecycle.

## Why `ResilientOIDCProxy` existed, and how it is split

At the research baseline, `ResilientOIDCProxy` combined three patches in one subclass; its remaining generic compatibility behavior now lives in `<../mcp_infra/authentik_auth/fastmcp_proxy.py>`.

| Current behavior                                                                                                                              | Why it exists                                                                                                           | Long-term disposition                                                                                                                                        |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Reclassify transient upstream refresh and selected Valkey refresh-persistence timeouts as retryable `503` instead of terminal `invalid_grant` | FastMCP wraps broad upstream refresh failures as `invalid_grant`; clients may permanently mark a connector disconnected | Keep as a narrow refresh patch; separately repair bearer-load/composite failure erasure, and delete each when a tested release preserves classified failures |
| Restore downstream DCR `client_id` after token swap                                                                                           | FastMCP returns the upstream verifier's `AccessToken`, whose `client_id` is the proxy's upstream client                 | Replace Haku identity use with opaque local `grant_id` context; retain `client_id` only as client-software metadata                                          |
| Verify and link Haku's current Operator by reading `_code_store` at the start of authorization-code exchange                                  | The current DCR-to-Operator link must be established before a downstream token escapes                                  | Keep Haku-owned and narrowly pinned until P5 replaces it with the Haku enrollment-adapter lifecycle                                                          |

PR [#3176](https://github.com/agentydragon/ducktape/pull/3176) split the implementation into independently named refresh, downstream-identity, and temporary raw-token-hook layers. P1 removes that generic raw-token hook entirely. Generic infrastructure now exposes a stock builder plus a lower-level composer around an already-constructed proxy; Haku alone constructs its `_VerifiedPrincipalOIDCProxy`. That Haku-owned checkpoint builds the resolver from FastMCP's public discovered `oidc_config`, verifies the signed upstream access token, and passes only a typed `VerifiedOidcPrincipal` to the DCR-to-Operator linker before FastMCP consumes the code. This completes current-link hardening only: it does not add the P2 adapter, an enrollment interaction, Agent/grant records, or a second authorization path. P5 replaces this private-store checkpoint when it activates the selected Haku adapter; downstream DCR identity restoration remains a focused shim while Airlock still consumes that compatibility behavior.

PR #3122 adds an arbitrary `oidc_proxy_factory` to `build_authentik_auth()`; the `origin/devel` baseline does not have that seam. The prototype should not retain it. That inversion gives product code a way to replace the protocol core while generic infrastructure still claims to know its concrete semantics. Instead:

- expose lower-level typed construction pieces;
- let generic consumers call a simple stock convenience builder; and
- let Haku explicitly construct a Haku compatibility adapter from those pieces.

PR #3159 replaced direct mutation of `proxy.client_registration_options.valid_scopes` with FastMCP's public `update_default_scopes()` and reuses the typed `proxy.oidc_config.jwks_uri` for direct JWT verifiers. Ducktape no longer issues a second discovery request in this builder. Keep those public APIs pinned by contract tests rather than reintroducing private registration-model access.

## Current FastMCP sequence versus the required Haku sequence

The position of the interaction is a load-bearing design decision.

### FastMCP today

```text
downstream /authorize
  -> FastMCP pre-IdP consent page
  -> consent POST
  -> redirect to Authentik
  -> Authentik callback exchanges code for raw upstream token response
  -> FastMCP immediately creates downstream code
  -> redirect to MCP client
```

At the built-in consent page, FastMCP knows the downstream client metadata, requested scopes, redirect URI, transaction, and browser binding. It does not yet know the authenticated upstream user. At the callback, it has a raw upstream token response but no public verified-principal result or application interaction before redirecting the browser. In FastMCP `3.4.4`, `OIDCProxy` defaults `verify_id_token=False`, and the stock proxy does not generate or check an OIDC nonce.

The discarded #3122 naming prototype collects an untrusted pending name at the first point and links it at code exchange by reading private stores. P1 now verifies the live link's access-token principal, but the prototype still has four structural problems:

- pending input is keyed by `client_id`, so concurrent flows and shared CIMD/preregistered IDs collide;
- the prototype assumes a post-IdP interaction and decodes raw upstream token material itself; P1 contains verification in Haku, while P2 proved that a pre-IdP Haku interaction plus token-exchange equality avoids that missing hook;
- a uniqueness or ownership error can arise after the browser has left the form, where the token endpoint cannot recover with a useful UI; and
- Haku state can commit at token exchange before FastMCP finishes persisting the token family.

### Required synchronous ceremony

The target flow above preserves this order: FastMCP validates downstream client/redirect/scopes/resource/PKCE and stores its transaction; Haku reserves the resulting exact public tuple and runs a one-time, authenticated browser interaction; FastMCP performs the untouched upstream IdP callback and creates the downstream code; token exchange verifies the MCP principal and canonical Operator equality; Haku creates the Agent and issuing grant; FastMCP persists the token family with opaque `grant_id` context; and the first successfully verified `tools/call` activates the grant.

This Haku page replaces the security role of FastMCP's generic consent screen. It must show client software, redirect hostname, scopes, and the action being authorized. FastMCP continues to own downstream client, redirect, resource, transaction, PKCE, callback, code, and token issuance. Haku owns its browser session, Agent name and global normalized-name reservation, explicit consent, one-time browser nonce, CSRF and Origin validation, canonical Operator equality, grant lifecycle, and request-time enforcement. With `require_authorization_consent="external"`, FastMCP `3.4.4` does not create or verify its built-in consent cookies.

### P2 adapter result and production gate

P2 passed the containment gate off production. On PR #3191's branch, the mounted full-flow contract target `//haku/console:test_mcp_agent_enrollment_integration` passed a forced-fresh BuildBuddy RBE run (`34646f6d-25f0-4b75-aeb3-9568f893abed`, outer `f2111abb-2bc5-4e36-9f31-c8a7f91d5ceb`). That target and its synthetic in-memory aggregate/store are intentionally not present on `devel`: merging them would create a short-lived second domain model rather than advance the terminal architecture. The branch remains executable research evidence for public authorize/`AuthorizationCode` tuple correlation, Haku session and interaction ownership, canonical browser/MCP Operator equality, opaque `grant_id` preservation through issue and refresh, first-`tools/call` activation, scope integrity, single-winner concurrent code exchange, and failure/reconciliation behavior. It contains no production wiring or live schema. P5's terminal contract still requires idempotent duplicate browser-POST results; the spike does not claim to prove that product behavior.

The production decision is **GO for the same bounded Haku-local composition contract in P5, after P3's canonical Operator cutover and P4's authorization-service consolidation are present**. It is not approval to transplant the spike's fake adapter, aggregate, store, or test wholesale, or to introduce an intermediate production authority. P5 must implement the final aggregate, persistence model, and permanent integration coverage once against the then-current code. C0 remains the permanent characterization coverage for shared FastMCP facts. In particular, `GrantCore.allowed_scopes` comes from the same provider-translated, client-facing effective scope set that FastMCP issues; raw upstream OIDC scope strings never become Haku authority. The implementation follows these containment rules:

- support exactly one pinned FastMCP version at a time;
- keep P1 `_code_store` read/delete as the sole private seam, version-pin the two protected context/scope hooks, and contract-test those facts;
- never parse FastMCP state, access `_transaction_store`, bridge the callback, copy code/token issuance, or override route construction;
- use exact validated tuple correlation only as a temporal reservation and locator, never as proof of browser or Operator authority;
- keep a closed tuple tombstoned for longer than the pinned FastMCP transaction TTL plus authorization-code TTL and safety margin;
- exercise authorize, deny, expiry, duplicate exact-tuple authorization, concurrent single-winner code exchange, refresh, response loss, retryable principal-verification unavailability, post-issuance Haku transition failure and reconciliation, revocation, scope narrowing, and first-call activation; and
- abandon the adapter for the quarantined-grant fallback if a future FastMCP upgrade would require Haku to own redirect validation, PKCE, callback handling, token generation, or broader private state.

Hydra or a custom Authlib authorization server is no longer a planned next step; it remains only an escalation if this bounded contract stops being viable and quarantine is unacceptable. FastMCP issue #4299 is likewise not a dependency. Only P5 may activate the adapter. Until that atomic cutover, production continues to use its one existing authorization path and persistence model; there is no adapter-backed auto-continue stage and no old/new dual write.

## Target Haku identity model

### What an Agent is

A Haku Agent is a local, operator-recognized actor. It is not proof of a physical process or installation. OAuth has no installation-attestation primitive, and a copied bearer makes two processes indistinguishable. The product may call a record “Claude.ai work connector” or “Claude on laptop,” but the security statement is only: an active credential binding authenticated as this Haku Agent.

Use a local immutable UUID for every canonical entity.

```text
Operator
  1 ─── * IdentityAnchor ─── * OidcIdentity
  1 ─── * Agent

Agent
  1 ─── * CredentialBinding
  0..1 ─ active CredentialBinding
  1 ─── * AgentNameReservation

CredentialBinding
  * ─── 1 Agent
  1 ─── 0..1 AuthorizationGrant (OAuth bindings only)
  1 ─── 0..1 StaticCredential (static bindings only)

AuthorizationGrant
  * ─── 1 ClientSoftware
  * ─── 1 OidcIdentity (authorization provenance)

EnrollmentInteraction
  0..1 ─ browser OidcIdentity  # required once AwaitingApproval is reached
  * ─── 1 ClientSoftware
  0..1 ─ pending AgentNameReservation
  0..1 ─ resulting AuthorizationGrant

ClientSoftware
  metadata only: DCR, CIMD, or preregistered client identity
```

Recommended records:

| Record                  | Important fields and invariants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Operator`              | `operator_id UUID`, lifecycle status; no protocol identifier as PK                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `IdentityAnchor`        | `anchor_id UUID`, immutable `operator_id`, configured `trust_domain`, stable external user key; unique `(trust_domain, stable_external_user_key)` so concurrent first logins at different issuers converge on one Operator                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `OidcIdentity`          | `identity_id UUID`, immutable `anchor_id`, exact verified `issuer`, `subject`, timestamps; unique `(issuer, subject)`. Relinking requires an explicit audited account-link migration, never an ordinary foreign-key update                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `EnrollmentInteraction` | random `interaction_id UUID`, client-software reference, exact client/redirect/S256 PKCE correlation tuple captured from public client/`AuthorizationParams` inputs only after `authorize()` succeeds, opaque upstream URL, exact requested scopes and presentation snapshot, expiry, and exactly one phase variant: `AwaitingBrowser`, `AwaitingApproval`, `Allowed`, `Exchanging`, or terminal `Completed/Denied/Expired/Failed`. `AwaitingBrowser` owns the one-time browser nonce; the transition to `AwaitingApproval` sets immutable verified browser `identity_id` and CSRF binding exactly once. Once set, every subsequent phase preserves that identity; expiry or failure may close an interaction before it is set. An optional resulting `grant_id` is valid only from `Exchanging`; denial creates no grant |
| `Agent`                 | `agent_id UUID`, immutable `owner_operator_id`, required `current_name_reservation_id` referencing a reservation owned by this Agent, `draft/active/disabled/deleted`, `last_seen_at`; current display/key derive from the reservation and the active credential derives from binding state rather than duplicated pointers                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `AgentNameReservation`  | required normalized non-empty immutable display string and globally unique normalized key; a relational union makes exactly one of `pending_interaction_id` or `agent_id` non-null. Browser approval owns it through the interaction; same-Operator token exchange atomically promotes it to the new Agent. Every activated current or historical name remains reserved; cleanup may delete a pending or never-activated draft reservation                                                                                                                                                                                                                                                                                                                                                                                |
| `ClientSoftware`        | local UUID, registration kind, OAuth `client_id`, validated redirect set/metadata hash, observed name/icon; all presentation metadata untrusted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `CredentialBinding`     | `binding_id UUID`, `agent_id`, enum-valued credential kind, `issuing/issued/active/revoked/expired/failed`, external family identifier, generation and optional `supersedes_binding_id`; exactly one subtype row and at most one active binding per Agent                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `AuthorizationGrant`    | `grant_id UUID`, unique `binding_id`, immutable authorizing `identity_id` provenance, required client-software reference, exact scopes, `issuing/issued/active/revoked/expired/failed`, timestamps and revoke reason; Agent owner and authorizing identity must resolve to the same Operator                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `StaticCredential`      | unique `binding_id`, secret reference or fingerprint and rotation metadata; Agent derives through the binding                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `ToolCallPrincipal`     | one-to-one relational union keyed by `tool_call_id`: exactly one of direct `operator_id` or submitting `binding_id`; an Agent and its owning Operator derive through the binding, which decision/execution revalidate instead of transferring queued authority to a replacement credential                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

An OAuth access token should carry or resolve only a stable `grant_id`; Haku then resolves `grant_id -> CredentialBinding -> Agent -> Operator` and verifies every row remains active. `agent_id`, display name, and owner do not need to be copied into the token. `client_id` remains valuable client-software audit metadata but is never the lookup key for the Agent.

Treat an OAuth Agent enrollment as one authorization aggregate, not as one denormalized database row or one impossible cross-request transaction. Each lifecycle transition is one locked Haku transaction. Browser approval creates only an interaction-owned name reservation; later, after the MCP principal resolves to the same canonical Operator, token exchange atomically promotes that reservation to the Agent and creates the `AuthorizationGrant` and `CredentialBinding`. The Agent owns its required name and, when introduced, its per-Agent auto-approval policy; the grant and binding own credential and token-family lifetime. Do not copy the name, owner, or policy onto OAuth records, and do not make a credential-lifetime row the canonical Agent.

FastMCP already has an internal `upstream_token_id` stable across refresh rotations. The local adapter must preserve Haku's opaque `grant_id` across the same token family without making Haku read that private model. Access and refresh token JTIs continue to rotate and are not grant IDs.

### Canonical Operator and multiple Authentik issuers

The correct external identity key is `(issuer, subject)`, but Haku intentionally has separate per-provider Authentik issuers for browser login and MCP proxy login. Both providers currently appear configured to use Authentik's `user_id` subject namespace. Equal `sub` values across issuers are not an OIDC guarantee, so Haku must make that Authentik-specific contract explicit rather than infer it.

Therefore:

- store each exact verified `(issuer, subject)` as its own immutable `OidcIdentity`;
- link multiple identities through one `IdentityAnchor` only inside a configured **identity trust domain** with a documented and integration-tested Authentik subject-mode contract, or through a dedicated stable Authentik user-ID claim verified at both issuers;
- atomically upsert the unique `(trust_domain, stable_external_user_key)` anchor so simultaneous browser and MCP first logins cannot create two Operators;
- keep `IdentityAnchor.operator_id` and `OidcIdentity.anchor_id` immutable. Account merge or link correction is an explicit audited migration because ordinary relinking would rewrite the apparent provenance of historical grants;
- reject issuers outside that allowlist even if their `sub` string matches; and
- on every create or reconnect, require the verified identity, `EnrollmentInteraction`, browser session, authorizing grant, and Agent owner to resolve to the same active Operator in one locked transaction; and
- block rollout and require an explicit account-link migration if an issuer's subject mode or stable-ID claim changes.

Never recover this identity by `jwt.decode(..., verify_signature=False)`. The adapter must validate signature, exact issuer, expected audience/upstream client, expiry, the chosen token type, and a non-empty `sub` before constructing a principal. If the design chooses the ID token, the adapter also generates, stores, sends, and verifies an OIDC nonce; an access-token design must not claim nonce validation it does not perform.

### Display-name rules

The user-visible Agent name is required, globally unique, and non-empty. Implement that contract as:

- preserve an NFC-normalized display string for presentation;
- trim leading/trailing Unicode whitespace and collapse internal Unicode whitespace runs to one ASCII space;
- reject control characters and bidirectional override/isolate characters;
- enforce a maximum of 80 Unicode scalar values after presentation normalization;
- compute `display_name_key` as NFKC plus Unicode case-folding of the normalized display string; and
- require `Agent.current_name_reservation_id` at the database boundary and enforce that it references a reservation owned by that Agent; derive the current display string and key from that row instead of copying them onto `Agent`.

Implement the required ownership cycle with a deferrable owned composite foreign key such as `(agent_id, current_name_reservation_id) -> AgentNameReservation(agent_id, reservation_id)`, plus the reservation's exactly-one-owner check, or an equivalent deferred database constraint. Token-exchange promotion, Agent/name creation, rename, and draft cleanup must commit atomically without a nullable Agent-name intermediate state.

Names remain reserved forever after an Agent becomes active, including historical names after rename and names of disabled or tombstoned Agents, so audit references never acquire a different meaning. Rename first inserts a new globally unique reservation and then switches the Agent's required `current_name_reservation_id` in the same transaction; it never deletes the old reservation. An expired allowed interaction releases its still-pending reservation. An expired never-activated draft Agent is not an established identity and releases its Agent-owned reservation during cleanup. Since global uniqueness can reveal existence and permits namespace squatting, return conflicts only inside authenticated operator interaction and rate-limit creation. If Ducktape becomes meaningfully multi-tenant, revisit global uniqueness; it is retained here because it is an explicit product requirement.

### Reconnect semantics

OAuth cannot prove that a new authorization is the same installation as an old one. On reauthorization:

- creating a new Agent creates a new name and grant;
- reconnecting an existing Agent must be an explicit operator choice on the Haku enrollment page;
- reconnect creates an `issuing` grant/binding with `supersedes_binding_id` and a generation while the current binding remains active. First-use activation compare-and-sets the recorded predecessor from `active` to `revoked` and the replacement to `active` in one transaction, under the unique-one-active-binding invariant. A delayed older reconnect whose predecessor is no longer active fails and revokes itself rather than superseding a newer binding; and
- Haku must never infer “same Agent” from `client_id`, `client_name`, redirect URI, or `(client_id, subject)`.

Static agents use the same Agent row and global naming rules. Bootstrap creates a `StaticCredential` binding for each existing configured static principal. Rotation creates a replacement binding and atomically supersedes the old one; a static token and OAuth token are never simultaneously active for one Agent. If two independently usable credentials are desired, model them as two named Agents.

## Typed callers without denormalized identity

Use a discriminated union for the canonical caller:

```python
@dataclass(frozen=True)
class OperatorPrincipal:
    operator_id: UUID


@dataclass(frozen=True)
class AgentPrincipal:
    binding_id: UUID


type ToolCallPrincipal = OperatorPrincipal | AgentPrincipal
```

The credential resolver derives the Agent, owning Operator, and display label from `binding_id`. Do not construct an identity object that repeats `agent.agent` as principal and display name and also carries a copied `operator_subject`. That makes several fields drift together.

Represent the union relationally as one required one-to-one `ToolCallPrincipal` row with nullable `operator_id` and `binding_id` foreign keys plus a check that exactly one is non-null. Do not store a string `principal_kind`, a separate Agent foreign key, copied owner, grant ID, or display label. For an Agent-originated call, `binding_id` is both the immutable submitting-credential provenance and the path to its Agent and Operator. Manual decision and execution revalidate that same binding/grant/Agent/Operator chain, so work queued under a revoked or superseded binding cannot execute later. Operator and Agent filtering join through the selected variant. If immutable historical labels later become an audit requirement, add an explicitly named `principal_display_name_snapshot` or an `AgentRenamed` event; do not smuggle a mutable label into the canonical principal. The current migration is allowed to drop past tool calls, so there is no need to invent historical identities while normalizing the model.

### One authorization boundary for multi-Operator, multi-Agent Haku

The persisted principal above and the request-time authorization capability are related but different. Resolve each request exactly once into this discriminated union:

```python
@dataclass(frozen=True)
class OperatorActor:
    operator_id: UUID


@dataclass(frozen=True)
class AgentActor:
    agent_id: UUID
    operator_id: UUID
    binding_id: UUID


type RequestActor = OperatorActor | AgentActor
```

`AgentActor.operator_id` and `binding_id` are transient, server-derived authorization context, not copied persisted identity fields. The only constructor for `AgentActor` verifies the complete active chain `credential -> grant/static credential -> binding -> Agent -> Operator`; a browser session constructs only `OperatorActor`. No route should independently combine a bearer `client_id`, session subject, display name, or optional tenant argument.

Put submit, read, poll, approve, deny, and execute behind one Haku tool-call application service. Its rules are:

- an `AgentActor` may submit calls on behalf of its owning Operator and read or poll only calls whose canonical Agent principal is that same `agent_id`;
- an `OperatorActor` may read all calls owned by that Operator, including calls from all of their Agents and direct Operator calls, but none owned by another Operator;
- auto-approval evaluates only Agent-originated calls and receives the full `AgentActor`. The first implementation may use one shared policy, while this interface permits later per-Agent policy without changing authentication;
- a call not auto-approved enters only its owning Operator's queue. Approval and denial select the call inside that Operator's scope and record the deciding Operator; an Agent can never approve its own call;
- approval and execution of an Agent-originated call revalidate its mandatory `ToolCallPrincipal.binding_id`; revoking, disabling, or superseding that binding before execution makes the call non-executable rather than transferring its authority to a replacement binding;
- backend OAuth credentials are selected for the resolved Operator only after the call and Agent ownership have been authorized; and
- websocket/event fanout is keyed by Operator, while Agent-facing result reads retain the narrower Agent predicate.

Repository methods must require an `OperatorActor` or `AgentActor`; there is no default, `None`, or unscoped ledger method. Apply ownership in the `SELECT`/`UPDATE ... FOR UPDATE` statement rather than fetching by ID and checking afterward. Keep raw SQLAlchemy sessions behind the repository, and include the canonical Operator/Agent IDs in every cache, pending-state, event, and idempotency key whose data is tenant-specific. The database foreign keys and exactly-one-principal check encode the ownership graph; the request actor provides the mandatory query scope without persisting a duplicate owner on every call.

Test this boundary as a reusable cross-product: at least two Operators, two Agents per Operator, direct Operator calls, and revoked/unlinked bindings. For every HTTP route, MCP tool, websocket/event path, decision transition, result poll, and backend-token lookup, assert own access succeeds and sibling-Agent, cross-Operator, ambiguous-credential, revoked-binding, and unlinked-client access fails. This matrix is the guard against a newly added path accidentally becoming global.

## Enrollment and issuance state machine

FastMCP KV state and Haku Postgres cannot share a transaction. Safety comes from a fail-closed state machine and reconciliation, not from pretending the writes are atomic.

```text
EnrollmentInteraction
  awaiting_browser -> awaiting_approval  # bind verified browser identity once
  awaiting_approval -> allowed
  allowed -> exchanging  # token exchange proved canonical Operator equality
  exchanging -> completed  # downstream token-family issuance succeeded
  awaiting_approval -> denied
  awaiting_browser/awaiting_approval/allowed/exchanging -> expired
  awaiting_browser/awaiting_approval/allowed/exchanging -> failed

AuthorizationGrant / CredentialBinding
  issuing -> issued
  issuing/issued -> active  # first successfully verified MCP request
  issuing/issued -> failed/expired
  active -> revoked/expired

Agent
  draft -> active  # atomically with first-use grant/binding activation
  draft -> [row removed]  # atomic abandoned-enrollment purge with its name
  active -> disabled/deleted
```

Rules:

1. Pending input is keyed by a random `EnrollmentInteraction.interaction_id`, never `client_id`. Its exact public tuple reservation remains unique while live and tombstoned for longer than FastMCP's transaction TTL plus code TTL and a pinned safety margin after closure.
2. `AwaitingBrowser` is bound server-side to the exact public client/`AuthorizationParams` inputs captured only after FastMCP's `authorize()` succeeds, the client/scopes/redirect presentation, expiry, and one-time browser nonce. It does not claim a FastMCP transaction identifier. Opening the page through an authenticated Haku session compare-and-sets it to `AwaitingApproval`, records the explicitly verified browser `OidcIdentity` and resolved Operator exactly once, and installs the one-time CSRF/form binding. Model these as discriminated phase variants rather than unrelated nullable optionals.
3. Allow and deny consume `AwaitingApproval` with compare-and-set. A duplicate browser POST returns the already-recorded outcome; denial creates no Agent, grant, binding, or credential.
4. Name uniqueness and the browser Operator's ownership of the interaction are decided in one locked browser-POST transaction. Allow creates only an interaction-owned pending name reservation; no Agent, binding, or grant exists yet. A conflict re-renders the form.
5. At token exchange, the verified MCP identity, browser session provenance, interaction, and resulting Agent owner must resolve to the same active Operator. In one locked transaction, create-new atomically promotes the pending reservation to a new `draft` Agent and creates its `issuing` binding/grant. Reconnect instead creates an `issuing` binding/grant with an expected predecessor and generation without disturbing the active binding.
6. FastMCP persists only opaque Haku `grant_id` context with the access/refresh token family. It does not copy `agent_id` or owner/display fields and the already-created authorization code contains no Haku grant context. Successful token-family issuance completes the exchanging interaction; persistence failure leaves it fail-closed for reconciliation and does not activate the grant.
7. Token-family persistence may best-effort mark the grant `issued`, but it does not activate the Agent. Any recorded initial access/refresh JTI is issuance and reconciliation evidence only; refresh rotation can make it stale, so it is neither current-family state nor an authorization input. On the first MCP request, FastMCP first verifies the bearer and yields `grant_id`; one Haku transaction promotes the new Agent from `draft` to `active` together with its `issuing/issued` grant and binding. Reconnect activation keeps the established Agent active while compare-and-setting the recorded predecessor binding's `active` status and installing the replacement; a stale out-of-order reconnect revokes itself.
8. This first-use rule defines response-loss semantics. If the token response is lost after the code is consumed, no MCP request arrives and no Agent becomes active. A same-code retry may fail under ordinary OAuth one-time-code rules and require reauthorization; it must never activate from token-family existence alone. An optional exact-request token-response replay cache may improve UX, but is not required for correctness.
9. A reconciler expires abandoned interactions and `issuing/issued` grants. It deletes an interaction-owned pending reservation when approval never reaches verified token exchange; after exchange, it deletes a never-activated draft Agent and its Agent-owned reservation after the enrollment/token lifetime. It never activates a grant merely because one store contains it. FastMCP records may expire through their existing TTLs or a later supported cleanup API; reconciliation does not add another private store seam.
10. Every authority-bearing MCP operation checks the grant, binding, Agent, and Operator status. Access-token load, including FastMCP's transparent refresh path, prechecks the Haku grant/binding before invoking FastMCP and postchecks it after FastMCP returns but before dispatch; explicit refresh applies the same two gates before returning credentials. The first valid `tools/call` may perform the activation transition above; later tool calls require `active`. The production composition must address both FastMCP `3.4.4` failure-loss points: `OAuthProxy.load_access_token()` catches all exceptions and returns `None`, and `MultiAuth.verify_token()` catches all source exceptions and falls through. Haku database, JTI-map, upstream-token-store, Postgres, or Valkey unavailability must remain a retryable service error, not a false `401` that causes clients to deauthorize.
11. Refresh requires an `issued` or `active` grant/binding before rotating tokens, preserves `grant_id`, and cannot activate an issuing grant or resurrect a revoked one. Refresh before first tool use leaves the grant `issued`.
12. Local revoke marks the Haku grant/binding inactive, making denial authoritative on the next access-token request and before any refresh rotation. FastMCP `3.4.4`'s public `revoke_token()` is not full family deletion: it removes refresh metadata for the supplied token and best-effort calls the upstream endpoint while other records otherwise expire by TTL. Do not add another private store seam merely for eager cleanup.

## Scope domains

Three scope domains must be typed separately even when some strings happen to match.

| Domain                            | Examples                                          | Meaning                                                    |
| --------------------------------- | ------------------------------------------------- | ---------------------------------------------------------- |
| Downstream MCP resource scopes    | `read`, `propose`, `decide`, tool-specific scopes | What the MCP client may do at this resource server         |
| Upstream identity/provider scopes | `openid`, `email`, `profile`, `offline_access`    | Claims/session behavior requested from Authentik           |
| Backend delegation scopes         | `openid email profile ak_proxy`                   | What an exchanged token may do at a backend proxy provider |

Replace one unqualified `valid_scopes` list with a policy object that states:

- scopes advertised and accepted from the MCP client;
- scopes required by the MCP resource;
- scopes sent to Authentik for identity and refresh behavior;
- any explicit mapping between downstream and upstream scope names; and
- scopes used only for backend token exchange.

Tests must prove scope preservation across initial authorization, local refresh, upstream refresh, and token swap. An upstream verifier's identity scopes must not overwrite the downstream grant's resource scopes, and Authentik property-mapping requirements must not silently broaden what the MCP client can do.

## Cookies and sessions

The consent cookie is not a credential the MCP HTTP client uses. The browser and MCP client are often different processes. FastMCP's built-in consent modes use their own short-lived consent cookie, but `require_authorization_consent="external"` skips that page and its cookie validation.

The synchronous Haku adapter must therefore create an opaque, short-lived, one-time interaction binding when it renders the pre-IdP enrollment page. A cookie plus form token is the simplest design; an equivalent server-bound capability is acceptable. It provides two narrower guarantees:

- double-submit CSRF: the submitted form token matches the browser-bound interaction state; and
- page binding: the browser that saw interaction `I` is the browser submitting the decision for `I`.

It does not prove that this browser process initiated the MCP client's outer OAuth request. A copied or cross-browser authorization URL must still pass Authentik authentication and show a visible, explicit Haku approval page; it must never inherit a silent allow decision.

The authority to create a draft Agent and issuing grant comes from the conjunction of:

- a valid, unexpired, allowed `EnrollmentInteraction` whose exact public client/redirect/PKCE tuple was captured only after FastMCP's public `authorize()` succeeded;
- an independently authenticated browser Operator, explicit allow decision, and valid CSRF/browser binding;
- a verified MCP-side principal that resolves to that same canonical Operator at token exchange; and
- one locked Haku transition that creates the draft Agent, issuing grant, and binding.

Activation is a later authority decision: FastMCP first verifies the bearer, the bearer resolves to that issued Haku grant, and the first verified MCP tool use atomically activates the draft Agent and its grant/binding. The completed enrollment interaction is audit provenance, not a request-time authorization input.

A cookie alone proves none of those. The Haku binding should contain only an opaque, short-lived, one-time interaction capability scoped to one path and interaction. Require `Secure`, `HttpOnly`, an intentional `SameSite`, `Cache-Control: no-store`, a restrictive CSP including `frame-ancestors`, and referrer protection. Do not store names, tokens, raw claims, or durable authorization in it.

Starlette `SessionMiddleware` uses signed client-side cookies. Its documentation says session contents are readable, though not modifiable. It is suitable for small browser login state, not secrets or centrally revocable high-value grants. Haku's operator session should have an explicit TTL and reauthentication policy; local logout clears that session but does not revoke MCP agents or necessarily terminate the Authentik SSO session.

## Revocation, logout, disconnect, and “deauth”

There is no reliable generic notification when Claude.ai, Claude Code, or another MCP client merely deletes local credentials or removes a connector. Silence is not revocation.

Keep these operations distinct:

| Operation                                 | Effect                                                                           |
| ----------------------------------------- | -------------------------------------------------------------------------------- |
| Haku browser logout                       | Clears Haku's browser session                                                    |
| Authentik OIDC logout/back-channel logout | Ends or signals an IdP login session where supported                             |
| RFC 7009 token revocation                 | A cooperating OAuth client or server asks an AS to invalidate a token            |
| RFC 7592 client-registration deletion     | Removes/manages a dynamic client registration where implemented                  |
| Downstream MCP account disconnect         | Deletes/revokes Haku's stored token for another MCP server                       |
| Haku Agent revoke/disable                 | Makes every credential binding for that Agent fail Haku authorization            |
| Haku grant revoke                         | Invalidates one OAuth relationship/token family while retaining the Agent record |

Haku needs its own authoritative revoke path:

1. atomically mark the grant/binding revoked in Haku;
2. make every access-token use and refresh check that state before tool dispatch or rotation so revocation is immediately enforced;
3. use FastMCP's public `revoke_token()` only for the individual-token cleanup it actually supports and best-effort upstream Authentik revocation when advertised;
4. allow remaining grant-associated FastMCP token records to expire by their existing TTLs or a later supported cleanup API instead of adding a private token-store seam; and
5. retain the Agent/audit record unless the operator explicitly deletes it.

Revocation is successful when Haku denies local access and refresh, not when FastMCP-family cleanup or the best-effort upstream call succeeds. Define and test a propagation target (normally one request, bounded only by any explicit verifier cache). Cleanup absence or failure leaves denial effective. `last_seen_at` can inform UI but must not infer deauthorization.

Authentik account disablement is a separate propagation path. A local Haku Operator disable is enforced on the next request. An Authentik-only disable is detected when the upstream token is next validated or refreshed unless an event sync is added; IdP logout alone is not revocation. Configure and document upstream/local access-token lifetimes so the worst-case Authentik-disable latency is no more than one hour, verify refresh rejection cannot leave the Haku grant active, and show that bound in the Connected Agents UI/runbook. Add Authentik event-driven disable synchronization later only if a shorter bound becomes a product requirement.

## OAuth state storage boundary

### Current behavior

FastMCP's `UpstreamTokenSet` and authorization-code models contain upstream access tokens, refresh tokens, ID-token/raw responses, and client binding data. FastMCP wraps only its internally created file store in `FernetEncryptionWrapper`. Ducktape's `<../mcp_infra/persistence.py>` returns raw `PostgreSQLStore` or `ValkeyStore` objects, and Airlock's `<../airlock/kv_store.py>` stores JSONB directly. Passing either as `client_storage` means the application layer does not add encryption.

Haku's outbound MCP OAuth associations in `<../haku/console/database_schema.py>` also store `client_secret`, access token, and refresh token as ordinary text columns. Airlock intentionally writes external-provider tokens to Kubernetes Secrets.

### Accepted posture

- Treat the OAuth Postgres databases, Valkey instances, Kubernetes Secrets, their backups, and their administrative readers as part of the private credential boundary.
- Preserve network policy, database credentials, RBAC, namespace isolation, and backup access controls appropriate for bearer credentials.
- Redact token values and OAuth form bodies from logs, metrics, exceptions, database inspection tooling, and undeclared test outputs.
- Keep FastMCP JWT signing keys, upstream OIDC client secrets, and browser-session secrets logically separate where practical; rotating one should not unexpectedly invalidate unrelated layers.
- Document whether each Airlock external credential is intentionally a singleton service credential or per-operator.

If the shared persistence helper can wrap custom stores with FastMCP's standard encryption without complicating key ownership or rotation, that is a reasonable default defense-in-depth improvement. It should be a separate optional change with a deliberate reconnect/migration plan, not a blocker for Agent identity or the authorization-interaction design.

## DCR and CIMD abuse controls

Supporting unknown client metadata is an internet-facing input surface, not only an interoperability feature.

Require:

- registration and authorization rate limits per source and transaction quotas;
- TTLs and garbage collection for DCR clients, transactions, codes, and abandoned grants;
- exact redirect validation and explicit loopback rules;
- CIMD fetch timeouts, response-size limits, redirect limits, public-address enforcement, DNS-rebinding defenses, and caching;
- autoescaped rendering of text client metadata, with names/icons treated as untrusted presentation;
- a visible redirect hostname and CIMD origin rendered as text on the Haku authorization page;
- no remote client icon by default; if icons are later supported, proxy and sanitize a strict raster-only media allowlist rather than embedding arbitrary URLs or SVG;
- CSP and no third-party active content on the authorization page; and
- tests for client-brand impersonation and confused-deputy redirects.

For high-value Haku deployments, preregistration-only is a credible policy, not a failure of architecture. Keep CIMD/DCR enablement an explicit server policy. As checked on 2026-07-13, Authentik's DCR feature request [#8751](https://github.com/goauthentik/authentik/issues/8751) was open, assigned to the `2026.8.0` milestone, and had no implementation PR. Reassess after an actual release and interoperability test; do not make it a dependency. Even if Authentik ships DCR, it will register client software, not create Haku Agents.

## Would a DCR-capable identity provider help?

It is a legitimate option, but DCR is not the missing Haku abstraction. DCR creates a client-software registration before a resource owner is present. It does not name an Agent, bind that Agent to an Operator, provide a post-login product interaction, or define Haku's grant/revocation lifecycle.

A sufficiently MCP-capable external authorization server could still simplify the generic case:

- MCP resource servers could use `RemoteAuthProvider` or direct JWT verification instead of running a local `OIDCProxy` facade;
- client registration, token revocation, issuer policy, and signing could live in one authorization server; and
- preregistration, CIMD, and DCR policy could be enforced consistently across MCP resources.

That does not require replacing Authentik cluster-wide. A lower-risk experiment would run a dedicated MCP authorization server federated to Authentik for human authentication. Only replace Authentik as the general IdP if a separate evaluation shows value beyond MCP DCR.

Keycloak is the most concrete self-hosted candidate found in this research. As checked on 2026-07-13, its official documentation reports support for RFC 7591 DCR and RFC 7592 registration management, and experimental CIMD support. The same official MCP guide classifies MCP `2025-11-25` support as partial because Keycloak does not yet process RFC 8707 `resource` indicators. Therefore it is not currently a drop-in standards upgrade over FastMCP for this repository. Its extension system might host a Haku ceremony, but then Ducktape would own a Keycloak extension and its lifecycle instead of a FastMCP adapter.

Ory Hydra is the more architecturally relevant alternative, but it is an authorization server rather than a replacement identity provider. Its documented design delegates login and consent to application-owned HTTP endpoints through opaque challenges, and its project advertises DCR and registration management. Haku could remain the owner of the post-Authentik naming/consent interaction and Agent database while Hydra owns clients, codes, token families, and revocation. That is a cleaner seam than moving Haku policy into a Keycloak authenticator or Authentik flow.

Hydra also adds a stateful public/admin service and database. This research found no official proof of current CIMD support or exact RFC 8707 behavior, so neither may be assumed. A Hydra path must pass the real MCP client/resource conformance matrix below before it can displace the local FastMCP adapter.

Evaluate any alternative authorization server against this pass/fail matrix before considering migration:

1. correct MCP protected-resource and authorization-server discovery when resources are mounted under paths;
2. interoperable preregistration, CIMD, and DCR with the actual Claude.ai, Claude Code, and other target clients;
3. RFC 8707 `resource` handling and exact audience validation that rejects cross-resource token replay;
4. public-client PKCE, redirect, loopback, DCR/CIMD SSRF, rate-limit, and registration-management behavior;
5. a post-authentication, pre-code application interaction receiving a verified principal, where remembered/skipped generic consent can never bypass Haku enrollment for a new grant or reconnect;
6. opaque Haku `grant_id` context preserved through code, access/refresh tokens, rotation, token load/introspection, and revocation;
7. supported lifecycle events or APIs for Haku's fail-closed activation and local revoke semantics;
8. stable Authentik-federated identity linkage, or a fully costed identity/policy/application migration;
9. GitOps ownership, backup/restore, upgrades, observability, and incident procedures; and
10. deletion of enough local proxy/compatibility code to justify adding or replacing a control-plane service.

If a candidate passes the protocol items but lacks the Haku interaction/grant items, it may still replace `OIDCProxy` for generic facades while Haku keeps its local adapter. That mixed deployment is acceptable only if the operational benefit exceeds the loss of one uniform MCP auth path. Do not switch IdPs solely to obtain DCR. Prefer a bounded Hydra-as-MCP-AS proof over a cluster-wide IdP migration if the FastMCP adapter crosses its containment threshold.

## Airlock's auth composition needs a focused correction

The following are code-level findings from the repository baseline, not claims that the live deployment was exploited or smoke-tested during this investigation.

### External-provider connection routes

`<../airlock/oauth/routes.py>` currently exposes `GET /oauth/authorize/{provider_name}` without an Airlock authorization dependency. The callback is necessarily reachable without an Airlock bearer, but the initiation endpoint should not be. A public caller can currently start a flow; if a provider user then completes that attacker-started flow, it can replace the singleton credential in the configured Kubernetes Secret.

The target shape is:

1. an authenticated operator-only `POST /api/oauth/providers/{provider}/connect` starts the flow;
2. the dependency requires a specific broker-management permission, rather than inheriting `read` accidentally;
3. the server creates a random, one-time, expiring state record bound to the initiating operator principal, provider, redirect URI, PKCE verifier, intended action, and expected current credential generation;
4. the callback remains public and authorizes only by atomically consuming that capability; possession of `state` does not prove that the callback browser currently has an Airlock operator session;
5. replacing an existing singleton credential requires an explicit confirmation or a separately authorized action, and compare-and-set on the expected generation prevents an older concurrent flow overwriting a newer credential; and
6. completion records who connected the provider, when, which scopes were granted, and which singleton credential was replaced.

The current code already binds provider and PKCE verifier and uses `dict.pop()` before the successful exchange path. Retain that one-time atomic consumption, then add expiry, bounds, initiating-operator/action/generation binding, and consumption on every terminal callback path including provider errors. Keep callback URLs/query strings out of logs and referrers. If Airlock runs multiple replicas or callbacks must survive a restart, use its existing private Postgres store.

The frontend should call the authenticated POST and follow the returned redirect URL. A bare `<a href="/oauth/authorize/...">` cannot attach its bearer or protect a state-changing initiation operation.

### Separate the four Authentik trust contracts

`<../cluster/k8s/agents/airlock/config.yaml>` configures the browser issuer as `/application/o/airlock/` and client ID `airlock-operator`. The OIDC proxy credentials come from the separate `airlock-oidc-proxy` provider. Claude Code is configured against the direct public-PKCE issuer `/application/o/claude-code-airlock/`, and OpenClaw uses `/application/o/openclaw-agent/`. `<../airlock/app.py>` currently passes the browser issuer to `OIDCProxy` with the proxy provider's credentials, then omits direct JWT verifiers in that proxy branch.

That configuration is trying to make one issuer field cover four roles. Model them as exact trust contracts:

```python
@dataclass(frozen=True)
class InteractiveMcpAuthorization:
    issuer: IssuerUrl  # /application/o/airlock-oidc-proxy/
    client_id: OAuthClientId
    client_secret: SecretStr
    downstream_scopes: frozenset[McpScope]


@dataclass(frozen=True)
class DirectJwtAuthorization:
    issuer: IssuerUrl
    audiences: frozenset[Audience]
    required_scopes: frozenset[McpScope]
```

| Contract                         | Exact issuer/client            | Intended authority                                                                              |
| -------------------------------- | ------------------------------ | ----------------------------------------------------------------------------------------------- |
| MCP-compatible interactive proxy | `airlock-oidc-proxy`           | `/mcp`, `propose` and `read` only unless its Authentik mappings deliberately add more           |
| Browser operator                 | `airlock` / `airlock-operator` | ordinary `/api`, `read` and `decide`; broker connection needs an explicit management permission |
| Claude Code direct public client | `claude-code-airlock`          | `/mcp`, `propose` and `read`                                                                    |
| OpenClaw machine                 | `openclaw-agent`               | `/mcp`, only its explicitly mapped machine scopes                                               |

The current Airlock builder advertises `decide` through the proxy even though `<../tf/gitops/airlock-oidc-proxy/main.tf>` maps only `openid`, `propose`, and `read`. The immediate correction is to stop advertising `decide` there unless policy explicitly changes the provider mapping.

Do not pass one permissive `MultiAuth` object to every route. Compose trust by surface:

| Surface                           | Accepted trust                                                                                                                                          |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/mcp`                            | Local `OIDCProxy` tokens plus exact direct verifiers for `claude-code-airlock` and `openclaw-agent`; browser operator JWT only if deliberately required |
| Ordinary `/api/*`                 | Exact `airlock-operator` browser JWT only                                                                                                               |
| Provider-connect POST             | Exact `airlock-operator` browser JWT plus broker-management permission                                                                                  |
| Provider callback                 | One-time server-side state capability only; no bearer requirement                                                                                       |
| `/healthz` and OIDC client config | Explicitly public, minimal responses                                                                                                                    |

Do not assume that sharing an Authentik signing key makes issuer and audience interchangeable. Add an end-to-end test for every intended path and a negative test proving that credentials accepted on one surface are rejected on the others.

### Keep Airlock safe without making it the permanent platform

Airlock still owns the live provider catalog, scope requests, singleton policy, Kubernetes publication, refresh scheduling, and UI. Correct its authentication boundaries and one-time callback state while it remains in service, but do not commit to a wholesale internal rewrite before deciding which consumers will migrate and whether Airlock will retire.

If a bounded live fix needs to replace hand-written authorization URL, PKCE, token exchange, refresh, or error parsing in `<../airlock/oauth/provider.py>`, use Authlib's OAuth client primitives rather than adding more protocol code. Provider-specific quirks should be small typed adapters around Authlib, and Kubernetes Secret writes remain an application side effect. This is a conditional maintenance rule, not a reason to polish Airlock into a permanent generic broker.

## A small browser OIDC relying-party package

Haku and Props already use Authlib plus Starlette sessions. Study Casino duplicates discovery, state cookies, token exchange, userinfo, and HMAC session encoding, then uses mutable `preferred_username` as the durable key. The right shared abstraction is a small relying-party helper, not a universal auth service.

Place it under a proposed neutral package such as `web_auth/oidc/` and keep its surface narrow:

- typed issuer, client, redirect, requested-claim, and session settings;
- Authlib Starlette/FastAPI client registration;
- a verified `OidcIdentity(issuer, subject, claims)` result;
- standard login/callback/logout helpers with state/nonce and safe redirect handling;
- consistent session-cookie defaults and a session-version hook; and
- test fixtures around `<../util/testing/mock_oidc.py>`.

Each application still owns:

- its routes and post-login destination;
- issuer allowlists and local-account linking;
- authorization policy;
- session lifetime and reauthentication requirements; and
- what logout means for that product.

Migrate Study Casino's durable identity from username to a local user UUID linked to `(issuer, subject)`. Preserve username only as presentation metadata. Haku and Props can adopt the helper only where it removes duplication; do not force a synchronized rewrite of already-correct application code.

## Typed authentication and credential modes

`AuthentikAuthConfig` currently has required OIDC-proxy fields plus optional exchange fields and an open-ended list of direct JWT trusts. Across the repository, strings and nullable fields allow invalid combinations that are discovered only at startup.

Use discriminated configuration models instead:

```python
class IncomingAuthKind(Enum):
    INTERACTIVE_OIDC_PROXY = auto()
    REMOTE_JWT = auto()
    STATIC_BEARER = auto()


class InteractiveOidcProxy(BaseModel):
    kind: Literal[IncomingAuthKind.INTERACTIVE_OIDC_PROXY]
    issuer: IssuerUrl
    client: ConfidentialClient
    scopes: McpScopePolicy


class RemoteJwt(BaseModel):
    kind: Literal[IncomingAuthKind.REMOTE_JWT]
    issuer: IssuerUrl
    audiences: frozenset[Audience]
    required_scopes: frozenset[McpScope]


class StaticBearer(BaseModel):
    kind: Literal[IncomingAuthKind.STATIC_BEARER]
    credential_ref: SecretReference
    principal: StaticAgentId
```

Define a separate enum-tagged union for outgoing `ClientCredentials` and `JwtBearerDelegation`. Use enums or constrained wrapper types for closed sets such as registration kind, grant status, principal kind, credential kind, and scope domain. A serialized YAML/JSON tag is necessarily text at the configuration boundary, but application code receives an enum member and exhaustive union, never compares bare string literals. OAuth scope values themselves remain strings because they are an extensible protocol namespace; the type should prevent a downstream MCP scope from being passed where an upstream identity scope is expected.

Do not create one giant union consumed everywhere. Separate incoming-auth modes, outgoing credential modes, and backend-delegation modes so a server can accept only configurations relevant to its role.

## Authentik and GitOps ownership

Authentik configuration currently spans blueprints, several Terraform modules, Kubernetes manifests, and generated/reflected Secrets. That is reasonable only if each provider/application has one declared owner.

A workable ownership rule is:

- Authentik blueprints own public or Authentik-internal objects that require no generated secret to leave Authentik, including shared flows, scope mappings, groups, and proxy outposts where appropriate.
- GitOps Terraform owns confidential OAuth providers whose generated client secret must be published to a Kubernetes workload or external controller.
- The consuming Kubernetes application owns only the reference to that generated Secret and its issuer/client configuration, never an independently copied credential.
- One central inventory records provider slug, application slug, protocol role, issuer mode, audiences, scopes, redirect URIs, source-of-truth path, secret consumers, and lifecycle owner.

The Kagent configuration demonstrates why this is necessary: `<../cluster/k8s/authentik/app/blueprints/kagent-sso.yaml>` declares a proxy provider/application/outpost named `kagent`, while `<../tf/gitops/sso-providers/provider_kagent.tf>` declares an OIDC provider/application with the same application slug for oauth2-proxy. Choose the active architecture, give the application one owner, and delete the orphaned competing definition after live verification.

Shared scope mappings also need one owner. A provider may reference a shared mapping, but must not recreate a semantically similar mapping under another controller. Add CI that rejects duplicate Authentik application slugs, provider names, client IDs where uniqueness is expected, and overlapping ownership declarations.

## Good patterns

### Compose protocol engines; own product interactions

Use FastMCP, Authlib, FastAPI, and Starlette for the protocol and web mechanics they implement. Let Haku and Airlock own their domain records and interaction text. The composition boundary is a typed verified result plus a lifecycle hook, not access to a library's internal stores.

### Model identity and credentials separately

A principal is a canonical local ID. A credential binding is evidence accepted for that principal. A display name is presentation. An OAuth client describes software. Keeping those records separate makes revocation, reconnect, renaming, and audit behavior explicit.

### Make authorization transitions explicit and idempotent

Use named states, unique transaction IDs, compare-and-set transitions, expiry, and reconciliation. Fail closed while preserving retryable server failures as `503`, rather than lying to clients with `invalid_grant`.

### Keep compatibility patches narrow

Every protected/private FastMCP use should have:

- one compatibility module;
- an exact version pin;
- a focused contract test against the behavior being repaired;
- an upstream tracking reference when one already exists, otherwise a local rationale;
- an owner and deletion condition; and
- no Haku domain code in generic `mcp_infra`.

### Render application HTML as application HTML

Put Haku's authorization/enrollment template in a separate Jinja file owned by Haku. Use Jinja autoescaping and pass structured values. Autoescape protects HTML contexts, not URL safety: render redirect/CIMD origins as text, generate form actions locally with `url_for`, and do not render remote client icons by default. Do not concatenate HTML in Python and do not manually escape fields before handing them to an autoescaping template. Add snapshot or browser tests for hostile client names, redirect origins, scopes, empty names, duplicate names, and CSRF failures.

### Audit decisions, not secrets

Record actor IDs, transaction/grant IDs, client-software IDs, requested and granted scopes, timestamps, state transitions, and reasons. Do not record codes, bearer tokens, refresh tokens, client secrets, full callbacks, or raw form bodies.

## Patterns to avoid

| Avoid                                                | Why                                                                                | Prefer                                                            |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Agent creation in DCR `/register`                    | DCR has no authenticated resource owner and identifies client software             | Haku-authenticated enrollment; create at same-Operator exchange   |
| Agent keyed by OAuth `client_id`                     | CIMD/preregistered IDs are shared and DCR IDs can be recreated                     | Local UUID Agent plus grant/binding records                       |
| `getattr()` probes of framework internals            | Converts version drift into runtime behavior                                       | Typed public hook or isolated pinned adapter                      |
| Arbitrary `oidc_proxy_factory` injection             | Generic infrastructure cannot safely promise semantics of an unknown protocol core | Explicit stock builder plus product-owned adapter construction    |
| Raw `id_token` decode without verification           | Confuses attacker-controlled claims with authenticated identity                    | Adapter-validated token and typed verified principal              |
| Optional-heavy universal auth config                 | Admits issuer/client/audience combinations that make no semantic sense             | Discriminated role-specific config                                |
| Bare strings for closed domain variants              | Typos and impossible states survive type checking                                  | Enums/discriminated unions                                        |
| One untyped scope list                               | Mixes MCP authority, identity claims, and backend delegation                       | Domain-specific scope policies                                    |
| Product HTML in `mcp_infra` or Python strings        | Couples generic protocol code to one UI and invites escaping mistakes              | Haku-owned Jinja template                                         |
| Manual HTML escaping                                 | Easy to double-escape or miss a context                                            | Template autoescape and safe structured rendering                 |
| FastMCP `custom_route` for authenticated UI          | It is outside `RequireAuthMiddleware`; parsed auth context is not enforcement      | Parent-owned FastAPI router with explicit dependencies            |
| Copying owner/name into a principal object           | Creates denormalized fields that drift                                             | Canonical IDs plus joins; explicit snapshots only for audit       |
| Treating logout or inactivity as revocation          | Client credential deletion is not observable                                       | Haku grant/Agent revoke state checked per request                 |
| Catching every upstream exception as `invalid_grant` | Transient outages permanently disconnect cooperative clients                       | OAuth errors as terminal; transport/storage failures as retryable |
| A central Ducktape auth microservice                 | Adds a network trust hop without removing application policy                       | Small libraries plus Authentik and app-owned domain state         |
| A full custom AS just to add a form                  | Reimplements the most security-sensitive OAuth machinery                           | FastMCP hook, quarantine UX, or pinned adapter                    |

## Architecture options and decision thresholds

| Option                                       | Protocol ownership                  | Product UX                                     | Upgrade cost                 | Security/audit shape                                                                            | Recommendation                                                     |
| -------------------------------------------- | ----------------------------------- | ---------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Stock FastMCP only                           | Upstream                            | Generic consent only                           | Lowest                       | Strongest supported boundary, no synchronous Agent naming                                       | Use for generic facades; insufficient for desired Haku UX          |
| Stock FastMCP plus quarantined unnamed grant | Upstream                            | Naming happens after token issue; client waits | Low                          | Public APIs and fail-closed resource policy                                                     | Safest fallback if upstream hook is unavailable                    |
| Public FastMCP interaction/grant hooks       | Upstream protocol, Haku interaction | Desired synchronous flow                       | Low if released upstream     | Best separation and testability                                                                 | Ideal future deletion condition; no current contribution planned   |
| Haku-local public composition adapter        | FastMCP plus one P1 private seam    | Desired synchronous flow                       | Medium per FastMCP upgrade   | Public tuple correlation; pinned `_code_store` and protected-hook contract tests                | P2 passed; implement in P5 after P3/P4 under the containment gate  |
| Ducktape custom `OAuthProvider`              | Ducktape owns full MCP AS           | Fully custom                                   | High and permanent           | Large security surface                                                                          | Only after the explicit escalation threshold                       |
| Authentik directly as MCP AS                 | Authentik                           | Authentik interaction                          | Medium if/when compatible    | Good central IdP policy, but no Haku grant ceremony and incomplete current MCP registration fit | Use for preregistered native servers, not Haku Agents              |
| Hydra MCP-only AS plus Authentik identity    | Hydra protocol, Haku login/consent  | Desired app-owned synchronous flow             | High initial, medium ongoing | Clean interaction boundary; current CIMD/resource-indicator behavior still unproven             | Formal fallback proof if the FastMCP adapter exceeds its threshold |
| Keycloak as IdP/AS                           | Keycloak plus custom extension      | Desired only through owned Java provider       | High initial and ongoing     | DCR/7592 and experimental CIMD, but current MCP 2025-11 is partial without RFC 8707             | No switch solely for DCR                                           |
| Replace Authentik cluster-wide               | New general IdP/AS                  | Depends on candidate                           | Very high                    | Migrates every app, identity, policy, secret, and runbook                                       | No MCP-only justification; evaluate as a separate program          |

The choice is not “FastMCP or FastAPI.” FastAPI composes routes and application policy; FastMCP or a custom `OAuthProvider` supplies the authorization-server state machine. Likewise, Authlib can implement that state machine but does not make owning it free.

## Recommended package and ownership layout

The exact filenames can change during implementation, but the dependency direction should not.

```text
mcp_infra/
  auth/
    models.py                 # typed incoming auth modes and scope domains
    authentik_metadata.py     # discovery, normalized issuers, typed metadata
    providers.py              # stock TokenVerifier/RemoteAuth/OIDCProxy/MultiAuth composition
    fastmcp_compat.py         # version-pinned shims only
    token_exchange.py         # Authentik JWT-bearer backend delegation
  persistence.py              # private store construction, optional standard wrapper

web_auth/
  oidc/
    models.py                 # OidcIdentity and RP config
    starlette_client.py       # Authlib integration and safe session defaults
    testing.py                # mock-IdP fixtures/helpers

haku/console/
  auth/                       # browser operator RP composition
  agents/
    models.py                 # Operator, Agent, Grant, CredentialBinding domain types
    repository.py             # Postgres operations and state transitions
    enrollment.py             # Haku interaction policy/service
    principal.py              # credential -> canonical principal resolution
    routes.py                 # authenticated management routes
    templates/
      authorize_agent.html
  mcp_auth/
    provider.py               # Haku composition of FastMCP + interaction adapter
    fastmcp_adapter.py        # owned, version-pinned private seam

airlock/                         # transitional live service; extract only for bounded fixes
  auth.py                     # separate MCP, browser API, and broker trust compositions
  oauth/
    broker.py                 # Airlock credential-broker policy
    routes.py                 # authenticated start + public one-time callback
    provider.py               # thin Authlib provider adapter
```

Do not create packages merely to match this tree. Extract a module when it gives one concept a clear owner or eliminates a real dependency cycle.

## Low-discussion runway after the FastMCP repin

The first successor PRs should improve invariants without choosing Haku's private FastMCP adapter, final schema, IdP, or storage design. A runway PR qualifies only when it:

- fixes one demonstrated invariant or characterizes one upstream compatibility boundary;
- introduces no Agent/grant/binding schema and no new FastMCP private-state dependency;
- changes no encryption, IdP ownership, or product enrollment choice;
- has a focused negative test or static type proof that exposes the current weakness; and
- leaves the code closer to the final typed authorization boundary rather than adding another parallel path.

### Landed baseline

The following work is now on `devel` and should be treated as the starting point, not repeated:

- PR [#3139](https://github.com/agentydragon/ducktape/pull/3139) pins the retired proxy-header boundary: forged `X-Authentik-*` headers do not authenticate browser routes.
- PR [#3140](https://github.com/agentydragon/ducktape/pull/3140) closes same-Operator sibling-Agent reads with mandatory Operator-versus-Agent query scope while hardening the existing external-string persistence model.
- PR [#3142](https://github.com/agentydragon/ducktape/pull/3142) renders the existing operator OAuth callback with an autoescaping Jinja template in a separate file, plus CSP, no-store, and hostile-input coverage.
- PR [#3146](https://github.com/agentydragon/ducktape/pull/3146) repins the repository atomically to FastMCP `3.4.4` and its `fastmcp-slim` package layout.
- PR [#3145](https://github.com/agentydragon/ducktape/pull/3145) replaces duplicate string-tagged caller/scope DTOs with `OperatorActor | AgentActor`, uses stable Authentik `sub` rather than username as the Operator authorization identity, and injects Agent actors into FastMCP FunctionTools through `Depends`.
- PR [#3143](https://github.com/agentydragon/ducktape/pull/3143) types the supported OAuth storage lifecycle as `ValkeyStore | PostgreSQLStore | None` and removes production `hasattr(..., "setup")` probing.
- PR [#3149](https://github.com/agentydragon/ducktape/pull/3149) moves actor types to a Haku domain module, passes the complete actor to auto-approval, and injects `OperatorActor` into browser routes through FastAPI dependencies.
- PR [#3150](https://github.com/agentydragon/ducktape/pull/3150) replaces the remaining MCP content `getattr`/`hasattr` probing with concrete `CallToolResult` and `TextContent` variants.
- PR [#3152](https://github.com/agentydragon/ducktape/pull/3152) makes Haku MCP auth a `StaticMcpAuth | OAuthMcpAuth` union whose OAuth variant carries a required shared Postgres/Valkey store; generic consumers may still choose FastMCP's file/default store.
- PR [#3151](https://github.com/agentydragon/ducktape/pull/3151) models Grocy success/error rows as literal discriminated variants and removes frontend casts while preserving unknown-shape fallback.
- PR [#3154](https://github.com/agentydragon/ducktape/pull/3154) adds a hermetic FastMCP `3.4.4` integration test proving a second authorization for the same browser and client still presents consent when `require_authorization_consent=True`; a mutation to remembered consent makes the test fail.
- PR [#3156](https://github.com/agentydragon/ducktape/pull/3156) removes the fake empty Operator subject from non-`operator_oauth` execution paths and proves the stable authenticated subject reaches auth resolution.
- PR [#3157](https://github.com/agentydragon/ducktape/pull/3157) replaces raw approval-decision strings with a shared `StrEnum` while preserving the `"approve" | "deny"` JSON and generated TypeScript wire contract.
- PR [#3160](https://github.com/agentydragon/ducktape/pull/3160) adds the two direct Bazel dependencies required by the focused #3156 regression test; it contains no production change.
- PR [#3159](https://github.com/agentydragon/ducktape/pull/3159) uses FastMCP's public `update_default_scopes()` API and typed OIDC discovery result instead of mutating registration internals or issuing a duplicate discovery request.
- PR [#3162](https://github.com/agentydragon/ducktape/pull/3162) pins real RS256/JWKS verifier behavior across key selection, issuer variants, audience lists, required scopes, and negative trust cases.
- PR [#3172](https://github.com/agentydragon/ducktape/pull/3172) aligns Haku's TODO with the canonical Agent/grant/binding model and retires the prototype's `client_id -> display_name` direction.
- PR [#3174](https://github.com/agentydragon/ducktape/pull/3174) characterizes FastMCP `3.4.4` code/token ordering, DCR identity loss, refresh causes, swallowed storage/verifier failures, consent ownership, and public revocation limits.
- PR [#3176](https://github.com/agentydragon/ducktape/pull/3176) splits refresh retry, downstream DCR identity restoration, and the current Haku authorization hook into independently named compatibility layers without changing runtime behavior or adding state.

### Runway complete

The low-discussion runway is complete. The material Haku schema and adapter choices below are now approved: P2 is complete off production, and P3 is in progress.

Do not fold the 50 ms tool-call wait loop into this stamp queue. Its replacement should use a lost-wakeup-safe Postgres notification protocol: register the waiter, repeat the actor-scoped read, wake on the matching invalidation, and always perform a final actor-scoped read at the deadline. `LISTEN/NOTIFY` is an invalidation channel, not the durable source of truth.

### Clear findings just beyond the five-second queue

Airlock advertises `decide`, while the checked-in Authentik proxy provider maps only `openid`, `propose`, and `read`. Correcting that mismatch is likely right, but first write down which of Airlock's interactive proxy, direct MCP, Claude Code, and OpenClaw credentials needs each scope; do not infer the whole route trust contract from one list. Likewise, Airlock callback state should be consumed before provider errors, expire, remain bounded, and be consumed on all terminal paths. Both are high-confidence security work, but the former needs a trust-contract check and the latter is a larger state-machine change, so discuss them after the smaller queue rather than presenting them as instant stamps.

Closed PR [#3085](https://github.com/agentydragon/ducktape/pull/3085) contains only a historical `debug/` security review. Its high-severity `x-authentik-*` fallback finding is already fixed on `devel`, including the forged-header regression behavior, so leave the stale review note unmerged. A separate edge rule stripping the two retired identity headers is reasonable tiny defense-in-depth if desired; a namespace `NetworkPolicy` needs rollout validation and is not part of this five-second runway.

Splitting route trust and authenticating provider-connect initiation remain separate follow-up lanes. The approved Haku critical path is the P3 canonical identity cutover, then P4 service consolidation, then the P5 atomic schema/authorization cutover and bounded composition adapter.

### What to preserve from PR #3122 during the runway

Treat commit [`3e8f7a311`](https://github.com/agentydragon/ducktape/commit/3e8f7a311ecf37edd7030db5c31d8ee769d8a888) from PR [#3122](https://github.com/agentydragon/ducktape/pull/3122) as a source-material parts bin, not a branch to rebase or merge.

| Disposition                    | Pieces                                                                                                                                                                                                                       | Reason                                                                                                                   |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Preserve as security contracts | One DCR client cannot be rebound across Operators; unlinked clients fail closed; static and OAuth client namespaces cannot collide; multiple clients remain distinguishable; every Operator query keeps its tenant predicate | These are correct multi-Operator/multi-Agent invariants independent of the final schema                                  |
| Translated on `devel`          | Agent-facing call reads add the current client principal to the already-required Operator ownership predicate through `AgentActor`                                                                                           | A DCR `client_id` is a valid current credential-binding discriminator even though it is not the Agent primary key        |
| Reuse with the final flow      | Haku-owned Jinja template, autoescaping/CSP and hostile-input tests, required/non-empty/unique-name tests, real authorization-code fixture, Connected Agents/filter TODO, and discriminated-union intent                     | Useful product and test assets, but landing them alone creates dead code                                                 |
| Supersede                      | DCR `client_id` as Agent primary key; pending names keyed only by `client_id`; copied caller display/owner fields; raw upstream token decoding; generic `oidc_proxy_factory`; private-store lifecycle choreography           | These encode the prototype architecture and either collide across concurrent/shared-client flows or denormalize identity |

The key distinction is that `client_id` restoration from PR #3121 remains necessary on FastMCP `3.4.4` to identify the presenting downstream credential today. Preserve it as a version-pinned compatibility shim and as input to `AgentActor` resolution. In the final model, translate that credential evidence to `grant_id -> binding_id -> agent_id -> operator_id`; never promote the client registration itself to the Agent.

## Concrete migration plan

Each production change should be a reviewable PR with its own acceptance tests. The requirement groups after the proposed stack are a detailed checklist, not an alternative set of same-numbered PRs. Do not combine the FastMCP upgrade, Haku schema rewrite, Airlock security correction, and Authentik ownership cleanup into one change.

### Approved work stack and current status

The requirement groups below describe what the stack must satisfy. Execute the Haku core as this dependency chain:

```text
P0 plan/prototype disposition
 |
 v
C0 FastMCP characterization -> P1 live verified-principal hardening
                                      |-> P2 off-production adapter gate --------\
                                      \-> P3 canonical Operator -> P4 service ---+-> P5 atomic Agent/auth cutover
                                                                                       -> P6 Connected Agents
                                                                                       -> P7 audit filter
                                                                                       -> P8 lifecycle slices
                                                                                       -> P9 per-Agent policy

P3 + P6 -> G1 per-Operator Google connection -> G2 remove Haku's Airlock dependency

A1 minimum live Airlock hardening is an independent lane; retirement remains a later program.
```

1. **P0 — complete: accept the plan and retire the prototype.** PR #3122 is closed as superseded without rebasing or merging; commit `3e8f7a311` remains source material, and PR #3172 updated `<../haku/console/TODO.md>`. No production code moved from #3122.
2. **C0 — complete: pin the exact FastMCP `3.4.4` compatibility facts in tests.** PR #3174 characterized raw upstream-token availability and code-persistence order, downstream DCR-client-ID loss, refresh cause preservation, storage failures erased by `OAuthProxy.load_access_token()`, verifier exceptions swallowed by `MultiAuth`, external-consent browser/CSRF ownership, and the limits of public `revoke_token()` without changing production behavior.
3. **P1 — complete: verify the current Agent-OAuth principal.** The live Haku-owned checkpoint now resolves the signed upstream access token to a typed principal after validating signature, exact issuer, scalar audience and authorized-party client, expiry/issued-at, Bearer token type, and non-empty `sub`, then runs the existing DCR-to-Operator link before FastMCP issues the downstream token. Invalid identity is terminal and consumes the owned code; transient JWKS failure returns retryable `503` without consuming it. Authentik-compatible DCR/PKCE coverage proves the access-token subject wins even when the ID-token subject differs. This hardens the one existing authority without new state or a parallel path; P5 reuses the resolver at cutover. It does not claim nonce validation or implement P2/P5 enrollment behavior.
4. **P2 — complete off production: pass the Haku FastMCP adapter feasibility and contract gate.** The isolated mounted-flow spike preserved on closed non-merge PR [#3191](https://github.com/agentydragon/ducktape/pull/3191) proved that public `authorize()`/`AuthorizationParams` and public `AuthorizationCode` tuple correlation preserve FastMCP's validated client/redirect/resource/PKCE machinery without state parsing, callback interception, `_transaction_store`, copied issuance, or route overrides. P1 `_code_store` read/delete is the sole private seam; two protected hooks carry opaque `grant_id` context and enforce scope integrity. On that branch, `//haku/console:test_mcp_agent_enrollment_integration` covers browser session/nonce/CSRF/Origin, canonical Operator equality, safe redirect-host presentation, naming, tuple tombstoning, single-winner concurrent code exchange, code/token/refresh, response loss, retryable JWKS unavailability, post-issuance Haku transition failure and reconciliation, revocation, and first-call activation. The fake adapter and in-memory domain model are deliberately not merged; P5 implements the final production aggregate and durable integration coverage once, and still owns idempotent duplicate browser-POST behavior. Production is GO for the bounded composition contract in P5 after P3 and P4. Retryable refresh and downstream DCR-client-ID restoration remain independently named shared compatibility shims because Airlock also consumes the latter.
5. **P3 — make a live canonical Operator cutover.** Add `Operator`, `IdentityAnchor`, and `OidcIdentity`, atomically resolve browser and MCP identities through the configured Authentik trust-domain anchor, and replace authorization use of raw subjects/usernames with the local Operator UUID across browser sessions, Operator OAuth associations, current Agent links, tool-call/event ownership, and static-Agent resolution. Migrate rows only where the verified stable key is unambiguous; require reconnect or explicit account linking otherwise. This is a focused live identity migration, not a dormant schema PR. Prove two issuers for one configured identity converge while equal `sub` values outside the trust domain do not.
6. **P4 — consolidate the existing tool-call authorization service without changing persistence.** Put submission, Agent/Operator reads, policy evaluation, decision, execution, and event publication behind one application service; remove route-local identity reconstruction and public unscoped ledger methods. Keep using the one already-live DCR mapping and row format until P5, but run the two-Operator × two-Agent cross-product through every HTTP/MCP/event/decision/execution path. This reduces P5's review surface without creating a replacement authority, shadow write, or migration-only model.
7. **P5 — make one atomic schema, enrollment, and authorization cutover.** Reuse P1's live verified-principal resolver, activate the adapter that passed P2, and add and activate `EnrollmentInteraction`, `Agent`, `AgentNameReservation`, `ClientSoftware`, `AuthorizationGrant`, `CredentialBinding`, and the `operator_id | binding_id` `ToolCallPrincipal` union together; adapt #3122's Jinja/CSP/hostile-input assets in the same change. Browser approval creates only an interaction-owned unique name reservation. At verified same-Operator token exchange, atomically promote that reservation to a newly created non-null-named Agent and create its grant and binding without denormalizing them onto one row; reconnect creates the new grant/binding within the same aggregate boundary. Carry only `grant_id` through the token family and activate it on first verified MCP use. Switch static and OAuth callers to canonical Agents/bindings, require binding-scoped authorization for every Agent tool call, and revalidate it at decision and execution. Invalidate the old authorization path through Haku state, let inaccessible FastMCP records expire by TTL or later supported cleanup, delete the old DCR-to-Operator mapping/path, require existing OAuth clients to reconnect, bootstrap static Agent names with global conflict validation, and drop past tool calls as allowed. Global normalized name reservations, the required current-name reference, owner consistency, state transitions, and reconnect predecessor generation are database invariants. This is deliberately the one larger PR: splitting it across two live authorization authorities creates the forgotten-permission paths this design is meant to remove.
8. **P6 — ship Connected Agents as one read-only vertical slice.** Add the Operator-scoped query and its minimal trusted-console UI together, showing Agent/client/scopes/status/created/last-seen/reconnect data. Reuse suitable #3122 copy and visual fixtures, but derive labels through canonical joins rather than a copied `caller_display_name`.
9. **P7 — ship Agent-filtered audit as one vertical slice.** Add the backend Agent filter and UI control together. Every query starts with authenticated Operator ownership before applying the Agent predicate; frontend filtering is presentation only, and the backend cross-tenant matrix remains authoritative.
10. **P8 — expose lifecycle operations as small vertical PRs.** Land API, UI, audit event, and negative authorization tests together for each operation family: revoke/disable first, rename and historical-name reservations second, then tombstone-delete and reconnect-history cleanup. The enforcement paths already exist from P5; these PRs expose them without redesigning authentication.
11. **P9 — add per-Agent policy only after the common lifecycle is stable.** Key policy by canonical Agent, pass it through the existing typed actor/application service, and prove it cannot broaden another Agent's or Operator's authority. This does not change OAuth identity or routing.

The later Google/Airlock lane is separate:

1. **G1 — make Google a Haku-owned per-Operator downstream connection.** Implement trusted-console connect/status/reconnect/revoke, private Haku token storage/refresh, and Operator selection at execution. It is neither MCP Agent enrollment nor an Agent-held credential.
2. **G2 — remove only Haku's Airlock dependency.** Delete `haku_console_google`, its Secret publication and External Secrets mirror, and the console token mount after G1 is live-proven. Do not couple this to unrelated Airlock consumers.
3. **A1 — keep live Airlock safe meanwhile.** Its exact issuer/client route composition and one-time provider callback state may be corrected independently. Do not first rewrite Airlock into a permanent Authlib platform or start its broader retirement.

### Requirement group 0: security and protocol baseline

1. **Complete:** PR #3146 atomically repinned Python, Bazel, Nix, and the `fastmcp_slim` package layout to FastMCP `3.4.4`. Keep the exact-version compatibility matrix as an upgrade gate.
2. **Complete current-state hardening:** PR #3140 closed same-Operator cross-Agent reads; PR #3145 then replaced the duplicate caller/scope DTOs with a typed `OperatorActor | AgentActor`. P3/P5 replace the existing string persistence keys with canonical IDs without introducing a second live model.
3. **Complete:** PR #3139 proves forged retired identity headers remain untrusted. PR #3154 adds the repeated-consent test for the repinned confused-deputy fix, and PR #3162 pins the direct RS256/JWKS trust contract; keep redirect and PKCE validation FastMCP-owned.
4. **Complete:** PR #3174 pins the three former `ResilientOIDCProxy` compatibility facts; PR [#3176](https://github.com/agentydragon/ducktape/pull/3176) gave refresh retry, downstream identity restoration, and the temporary Haku hook independent names and deletion paths. P1 deletes the generic raw-token hook and makes the live Haku-owned checkpoint consume only a verified access-token principal.
5. **Pending and discussion-worthy:** apply the minimum live Airlock security correction: separate issuer/client trust contracts and protect external-provider flow initiation after documenting the route/credential matrix. Add live-compatible integration tests, but do not expand Airlock as Haku's hub or refactor it into a permanent broker by default.
6. **Complete:** treat Postgres/Valkey/Kubernetes Secrets as the accepted private storage boundary. PR #3143 removes lifecycle duck typing without adding encryption. PR #3152 makes Haku's OAuth composition carry a non-optional shared store and nests it under the OAuth config. Optionally evaluate FastMCP's standard storage wrapper in an independent later change; do not block Agent identity work on encryption.

### Requirement group 1: shared vocabulary and configuration

1. Introduce typed issuer, audience, scope-domain, incoming-auth, and outgoing-credential models under `mcp_infra`.
2. Keep metadata discovery, provider composition, token exchange, and compatibility code split across `<../mcp_infra/authentik_auth/{oidc_principal,provider,token_exchange,fastmcp_proxy}.py>`.
3. Replace optional-field construction with discriminated configuration at consumers atomically; do not add transitional parallel APIs within the monorepo.
4. Do not carry PR #3122's `oidc_proxy_factory` forward; Haku will explicitly own construction of its adapter.
5. Preserve the two valid shared service patterns as named constructors: credentialed facade and identity-preserving JWT-bearer delegation.

### Requirement group 2: local FastMCP composition adapter

1. P2 proved the off-production composition contract on non-merge PR #3191; its branch-only fake adapter is evidence, not production source material. FastMCP validates and stores the downstream transaction through public `authorize()`; after that call succeeds, Haku temporally reserves the exact `(client_id, redirect_uri, S256 code_challenge)` tuple from the public client/`AuthorizationParams` inputs and runs pre-IdP consent in its own authenticated browser session. FastMCP's callback stays untouched; the exchange wrapper performs Haku checks while delegating issuance unchanged to FastMCP. Public `AuthorizationCode` data correlates the tuple, and canonical Operator equality is required before Agent/grant creation. Do not install an auto-continue production adapter or write live Agent/grant/binding state before P5.
2. Define an explicit verified-principal resolver. Before extracting `sub`, validate signature, exact issuer, expected audience/upstream client, expiry, chosen token type, and required claims. If identity comes from an ID token, generate/store/send/check an OIDC nonce; if it comes from an access token, do not claim nonce validation.
3. Define the interaction contract around the independently authenticated browser Operator, an immutable public tuple view, a one-time `EnrollmentInteraction`, and structured allow/deny completion. The tuple is a locator and duplicate guard, not authority. Preserve its closed tombstone for longer than the pinned FastMCP transaction TTL plus code TTL and a safety margin.
4. Define opaque `grant_id` context preserved through access/refresh-token creation, refresh rotation, and token load. Haku grant denial is authoritative for immediate access and refresh revocation; FastMCP's public `revoke_token()` is individual-token cleanup, not full family deletion. Outside the compatibility adapter, product code never reads private FastMCP stores; inside it, P1 `_code_store` read/delete is the sole permitted private seam.
5. Define end-to-end bearer failure classification. The adapter-owned composite continues on a clean verifier `None`, but preserves classified operational failures through both `OAuthProxy.load_access_token()` and `MultiAuth.verify_token()` so store outages produce retryable service errors rather than false authentication failures.
6. Define cancellation, retry, expiry, exception, duplicate-submission, and concurrent-interaction semantics locally. A callback with unspecified lifecycle is not a sufficient API.
7. Define persistence success/failure evidence, response-loss behavior, reconciliation, and first-verified-`tools/call` activation; prove no token-family existence alone activates a grant.
8. Pin and diff-test P1 `_code_store` plus the protected context/scope hooks. Do not patch or bridge the callback, parse upstream state, access `_transaction_store`, copy issuance, or make an upstream contribution as part of this work.
9. The decision is GO for the exact bounded adapter contract in P5 after P3 and P4, with quarantine as the fallback if a repin crosses the containment threshold. Do not proceed on the assumption that issue #4299 will solve it.
10. If the adapter exceeds its containment threshold, run the bounded Hydra-as-MCP-AS proof with Authentik upstream. Do not deploy it unless the pass/fail matrix shows current-client CIMD/DCR interoperability, exact resource binding, clean Haku grant lifecycle, and enough deleted local complexity. Current Keycloak documentation fails the same gate on RFC 8707, so a cluster-wide Keycloak migration is not this fallback.

### Requirement group 3: normalize Haku identity and audit data

1. P3 adds and activates local `Operator`, `IdentityAnchor`, and `OidcIdentity`. P5 atomically adds and activates `EnrollmentInteraction`, `Agent`, `AgentNameReservation`, `ClientSoftware`, `AuthorizationGrant`, `CredentialBinding`, `StaticCredential`, and `ToolCallPrincipal`, all with UUIDs and database invariants.
2. Do not leave the Agent/grant/binding schema dormant or populate canonical identities or grants from the old hook. P5 is the first production writer of those records: it reuses P1's verified-principal resolver and activates the adapter accepted by P2 in the same cutover.
3. Make the Agent's current display name non-null and normalized non-empty, and reserve every activated current or historical normalized name globally in Postgres.
4. Define the configured Authentik identity trust domain and atomically link browser and MCP issuer-scoped identities through the unique stable external-user anchor. Make identity-to-Operator links immutable outside an explicit audited migration.
5. Replace stringly `ToolCallCaller` data with persisted `OperatorPrincipal | AgentPrincipal` and request-time `OperatorActor | AgentActor` unions. Make the credential resolver the only constructor for actors and resolve owner/display data through repositories.
6. Put submission, read/poll, policy evaluation, decision, and execution behind one tool-call application service. Remove public unscoped ledger methods and route-local identity reconstruction.
7. Store exactly one `ToolCallPrincipal` variant for every call: direct `operator_id` or Agent `binding_id`, never both. Derive Agent and owner through the binding, copy no owner/grant/display fields, and revalidate the binding at decision and execution.
8. Bootstrap each existing static-agent configuration into a named Agent plus static binding, validate global name uniqueness at startup/migration, and define atomic token rotation. Drop existing past tool calls during this migration as explicitly permitted rather than preserving denormalized identities.
9. Run the two-Operator × two-Agent cross-product against every HTTP/MCP/event/decision/execution path before switching production reads to the new model.

### Requirement group 4: implement Haku enrollment at the chosen seam

1. Keep PR #3122's naming/template concern in Haku, but replace its private-store choreography. Do not add the prototype's generic `oidc_proxy_factory` or a Haku-specific lifecycle hook to `<../mcp_infra/authentik_auth/provider.py>`. Replace the stale consent-in-SPA and `client_id -> display_name` design in `<../haku/console/TODO.md>` before it guides implementation.
2. After client metadata registration and FastMCP's public authorization validation, create a one-time `EnrollmentInteraction` in `AwaitingBrowser` before redirecting to Haku. Authenticate Haku's independent browser Operator session, compare-and-set the interaction to `AwaitingApproval` while binding that verified identity exactly once, and show the Haku-owned “register this Agent” page before forwarding the browser to the MCP-side Authentik flow. Identify the requesting client/scopes, state that the Agent will act as this Operator, require its globally unique name, and provide explicit authorize/deny actions. Explicit upstream-token verification happens later at downstream token exchange.
3. Add the Haku-owned Jinja template, autoescape, strict CSP, no-store headers, adapter-owned one-time browser/form binding, CSRF validation, and explicit allow/deny actions.
4. Key interactions by a random interaction ID plus public authorization-tuple context, never by `client_id` and never by parsed FastMCP state. Express `AwaitingBrowser | AwaitingApproval | Allowed | Exchanging | Closed` as a discriminated lifecycle: only `AwaitingBrowser` lacks browser identity, the transition out of it sets the immutable verified identity once, and every later applicable phase preserves it.
5. Present create-new and explicit reconnect-existing choices. The browser transaction validates global name uniqueness and creates only an interaction-owned pending reservation while conflicts remain correctable. At token exchange, require the verified MCP identity and browser interaction to resolve to the same active Operator, then atomically transfer that reservation to the newly created non-null-named Agent and create its grant/binding. Reconnect performs the same equality check before creating the replacement grant/binding and leaves the existing Agent name non-null.
6. Carry only stable `grant_id` context into the downstream token family and activate/reconnect only on its first successfully verified MCP request. Use generation plus predecessor compare-and-set so concurrent reconnects cannot activate out of order.
7. Enforce active Operator and eligible Agent/grant/binding state, scope, and operational-failure classification on every MCP request and refresh. Precheck the Haku grant/binding before invoking FastMCP bearer load or refresh—including transparent refresh—and postcheck it again after FastMCP returns but before returning credentials or dispatching work. A revoke before the call prevents refresh side effects; a concurrent revoke discards the returned credential and denies the request.
8. Add expiry/reconciliation for abandoned interactions and their pending reservations, never-activated draft Agents and their Agent-owned reservations, and `issuing/issued` grants. Let inaccessible FastMCP records expire by existing TTL or later supported cleanup rather than adding another private store seam.

### Requirement group 5: finish the Haku product lifecycle

1. Build an operator-only Connected Agents UI showing display name, client-software metadata, scopes, created/last-seen timestamps, status, and reconnect history.
2. Add grant revoke, Agent disable, Agent tombstone-delete, and rename operations with explicit semantics and audit records. Rename creates a new name reservation while every activated historical name remains reserved and audit foreign keys remain valid.
3. Add tool-call filters by canonical Operator and Agent. Every query must enforce the authenticated Operator predicate before applying an Agent filter.
4. Surface “last seen” as observation only; never label inactivity as disconnected.
5. Document that a client-side removal may be invisible and provide an operator-owned revoke action as the authoritative control.
6. Later, add per-Agent approval-policy configuration. The policy engine already receives `AgentActor`, so this phase changes policy data and UI rather than authentication or tenant routing.

### Requirement group 6: consolidate browser relying parties

1. Extract the small Authlib/Starlette OIDC RP helper from the common correct behavior in Haku and Props.
2. Migrate Study Casino from its custom discovery/state/session protocol and username identity to Authlib plus local UUID and `(issuer, subject)`.
3. Adopt the helper in Haku and Props only where tests prove equivalent behavior.
4. Give each application's browser session an explicit TTL, local logout behavior, and optional session-version revocation policy.

### Requirement group 7: keep Airlock bounded and plan Haku's later decoupling

This phase is not on the near-term Haku Agent-enrollment critical path.

1. Apply only the focused authentication, callback-state, revocation, and token-redaction fixes required to keep current Airlock consumers safe.
2. Inventory every Airlock hub caller, backend, and provider credential. Record which product owns each eventual migration; do not make Haku Console the accidental broker for unrelated Oura, BSC, or other credentials.
3. Design Haku's Google connection as a separate, per-Operator downstream-provider flow in trusted console chrome, with private Haku storage, refresh/status/reconnect/revoke semantics, and Operator selection at tool execution. Do not reuse MCP Agent enrollment or the singleton Secret as its identity model.
4. After that flow is proven, remove `haku_console_google` from Airlock and delete its Secret publication/mirroring path atomically.
5. Migrate Airlock's remaining MCP hub/approval callers to Haku Console or direct services only when equivalent identity, policy, audit, and operational behavior exists. Retire Airlock only when no callers or credential grants remain; that retirement is a separate future program.
6. Mine `<../x/agent_server/>` for prior-art invariants and tests when useful, but do not revive or clean it up as part of this migration.

### Requirement group 8: make Authentik ownership singular

1. Generate the provider/application inventory described above from blueprints and Terraform where possible.
2. Resolve Kagent's duplicate proxy-vs-OIDC ownership after verifying the deployed consumer.
3. Assign every shared scope mapping and provider application one controller and delete orphaned definitions.
4. Update `<../cluster/docs/mcp_oauth_authentik_notes.md>` for the current MCP registration preference order.
5. Add static duplicate/ownership checks and a live drift audit to the relevant GitOps validation.

### Requirement group 9: delete compatibility code

If Haku can later use released public FastMCP authorization-code inspection/consumption and typed token-context/scope hooks, delete the adapter's `_code_store` read/delete and protected-hook overrides plus their compatibility tests in the same change. Do not introduce the internal-model imports or arbitrary factory seam that this architecture rejects. This is a future cleanup condition, not a current upstream work item. After all browser RPs use the helper, delete Study Casino's custom session format. If bounded Airlock maintenance adopts Authlib, delete each replaced protocol helper immediately; otherwise prefer deleting the whole service after its final consumer migrates over rewriting it first.

## Verification and acceptance matrix

The architecture is complete only when these behaviors are tested at the appropriate layer.

| Area                   | Required acceptance cases                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Client registration    | Preregistered, CIMD, and DCR clients each work when enabled; a shared `client_id` can authorize multiple Operators and Agents without collision                                                                                                                                                                                                                                                                                                                                         |
| Concurrent enrollment  | Two interactions for the same client software remain isolated; duplicate names re-render in the correct browser; duplicate POST is idempotent; two reconnects activated out of order cannot let the stale binding win                                                                                                                                                                                                                                                                   |
| Identity               | Bad signature, wrong audience, missing/changed issuer, expiry, missing `sub`, and unverified decode fail; wrong issuer with matching `sub` is rejected; concurrent first provisioning at two configured issuers converges on one Operator; ID-token nonce is tested if that design is chosen                                                                                                                                                                                            |
| Tenant isolation       | Two Operators with two Agents each: Agents submit/read/poll only their own calls; Operators see/decide all and only owned calls; cross-owner identity/grant construction, browser-session/principal mismatch, sibling-Agent, cross-Operator HTTP, MCP, event, and token paths fail closed                                                                                                                                                                                               |
| Interaction security   | PKCE, redirect/resource, upstream state, expiry, missing/wrong browser binding, copied authorization URL, cross-site form POST, replay, concurrent interactions, deny/back/duplicate POST, exact-tuple reuse before its transaction-plus-code tombstone expires, and remembered/skipped-consent bypass are covered; the synchronous path shows one Haku consent page, not double consent                                                                                                |
| Rendering              | Hostile client name/icon/URI/scope and hostile Agent name cannot inject HTML, script, URL, style, or header content                                                                                                                                                                                                                                                                                                                                                                     |
| Scopes                 | Downstream MCP, upstream identity, and backend exchange scopes survive initial issue and refresh without broadening or collapse                                                                                                                                                                                                                                                                                                                                                         |
| Grant lifecycle        | Interaction `awaiting_browser -> awaiting_approval -> allowed -> exchanging -> completed`, or denied/expired/failed; the new Agent and its `issuing/issued` grant/binding become active atomically only on first verified tool use; denial creates no grant; lost token response never activates; abandoned pre-exchange approval cleans its pending name, abandoned post-exchange issue cleans the draft Agent/name; reconnect uses predecessor CAS; refresh cannot activate/resurrect |
| Tool authorization     | Every Agent submission records its binding; revoke, disable, or reconnect after submission but before approval/execution prevents that old-binding work from executing; unrelated active bindings remain unaffected                                                                                                                                                                                                                                                                     |
| Failure classification | Invalid/expired/random bearer produces `401`; Authentik rejection produces terminal OAuth error; Haku DB, JTI-map, upstream-token-store, DNS/timeout/5xx, Valkey, and Postgres failures produce retryable service errors without changing grant state or falling through to another verifier                                                                                                                                                                                            |
| Revocation             | Haku denial blocks the very next access and refresh use; revoked-before-start performs no FastMCP refresh, revoke-during-refresh returns no credential, and another grant remains active; absent or failed individual-token cleanup does not weaken denial; remaining grant-associated FastMCP token records expire by existing TTL or a later supported cleanup API                                                                                                                    |
| Resource isolation     | A Haku, Airlock, Grocy, or native-server token is rejected at every other resource; audience and RFC 8707 resource-indicator binding survive issue and refresh                                                                                                                                                                                                                                                                                                                          |
| IdP disablement        | Local disable is immediate; Authentik-only disable is bounded by the documented maximum one-hour token-validation/refresh window                                                                                                                                                                                                                                                                                                                                                        |
| Static agents          | Static and OAuth bindings resolve into the same canonical Agent domain; bootstrap/rotation leaves one active binding and copies no owner/name fields                                                                                                                                                                                                                                                                                                                                    |
| Airlock auth           | Operator, interactive proxy, Claude Code, and OpenClaw credentials validate only exact issuer/audience/scopes and only on intended route surfaces                                                                                                                                                                                                                                                                                                                                       |
| Airlock broker         | Anonymous initiation fails; capability state is one-time/expiring and binds initiator/provider/action/generation; stale concurrent completion cannot overwrite newer credentials                                                                                                                                                                                                                                                                                                        |
| ASGI composition       | Root metadata/protocol endpoints have their intended public status; mounted `/mcp` enforces its provider with correct `401/403`; parent REST dependencies apply only to parent routes; lifespan/callbacks work; no product route relies on `custom_route` for authentication                                                                                                                                                                                                            |
| Audit/privacy          | Decisions contain canonical IDs and scopes; no token/code/secret or full OAuth form appears in logs, metrics, traces, or test artifacts                                                                                                                                                                                                                                                                                                                                                 |
| Migration              | Past tool calls may be dropped; schema migration cannot create null/empty/duplicate Agent names; rename preserves every historical reservation; canonical records are never populated from unverified tokens; rollback behavior is documented                                                                                                                                                                                                                                           |

Run the framework-level matrix against the exact pinned FastMCP version and again before every FastMCP upgrade. Run browser flows with a hermetic OIDC provider and at least one real-client smoke test for Claude.ai or Claude Code before rollout.

## Disposition of Haku naming PR #3122

PR [#3122](https://github.com/agentydragon/ducktape/pull/3122) is closed as superseded and was neither rebased nor merged. Its retained commit remains source material, but its monolithic identity and lifecycle model is discarded. Build the phased successors from current `devel`; closing the PR did not delete the commit.

Keep or adapt:

- the separate Haku-owned Jinja template and autoescaping direction;
- the non-null, non-empty, unique display-name requirement and its tests;
- the move toward a discriminated caller union;
- the immutable DCR-client-to-Operator binding, unlinked-client failure, client-namespace separation, and multi-client routing tests;
- the operator-facing Connected Agents and tool-call-filter TODO; and
- integration fixtures that exercise a real authorization-code exchange.

Rework before treating it as durable:

- replace `client_id` as Agent primary key and pending-state key;
- retain the currently deployed DCR client only as the credential discriminator, and scope Agent-facing reads by both its Operator and client principal until P5 replaces that path atomically;
- separate EnrollmentInteraction, Agent, AgentNameReservation, ClientSoftware, AuthorizationGrant, and CredentialBinding;
- resolve a verified upstream principal instead of decoding raw `id_token` data;
- remove `oidc_proxy_factory` and scattered access to FastMCP private stores/methods;
- perform naming/reconnect while the verified Operator is known and browser errors are recoverable;
- make tool-call principals carry canonical IDs rather than copied owner/display data; and
- enforce grant/binding status on every request, including refresh and post-revoke use.

## References

Upstream references used for this plan:

- [MCP authorization specification, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [MCP Python SDK `OAuthAuthorizationServerProvider`, v1.26.0](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/src/mcp/server/auth/provider.py)
- [MCP Python SDK authorization routes, v1.26.0](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/src/mcp/server/auth/routes.py)
- [FastMCP OIDC proxy documentation](https://gofastmcp.com/servers/auth/oidc-proxy)
- [FastMCP OAuth proxy documentation](https://gofastmcp.com/servers/auth/oauth-proxy)
- [FastMCP `MultiAuth` documentation](https://gofastmcp.com/servers/auth/multi-auth)
- [FastMCP remote OAuth documentation](https://gofastmcp.com/servers/auth/remote-oauth)
- [FastMCP HTTP deployment and custom-route behavior](https://gofastmcp.com/deployment/http)
- [FastMCP v3.4.4 release](https://github.com/PrefectHQ/fastmcp/releases/tag/v3.4.4)
- [FastMCP confused-deputy consent fix, PR #3960](https://github.com/PrefectHQ/fastmcp/pull/3960)
- [FastMCP custom-consent renderer request, issue #4299](https://github.com/PrefectHQ/fastmcp/issues/4299)
- [FastMCP stable token subject request, issue #4266](https://github.com/PrefectHQ/fastmcp/issues/4266)
- [FastMCP v3.2.4 consent implementation](https://github.com/PrefectHQ/fastmcp/blob/v3.2.4/src/fastmcp/server/auth/oauth_proxy/consent.py)
- [FastMCP v3.2.4 built-in consent UI](https://github.com/PrefectHQ/fastmcp/blob/v3.2.4/src/fastmcp/server/auth/oauth_proxy/ui.py)
- [FastMCP v3.2.4 `OIDCProxy`](https://github.com/PrefectHQ/fastmcp/blob/v3.2.4/src/fastmcp/server/auth/oidc_proxy.py)
- [FastMCP v3.4.4 consent implementation](https://github.com/PrefectHQ/fastmcp/blob/v3.4.4/fastmcp_slim/fastmcp/server/auth/oauth_proxy/consent.py)
- [FastMCP v3.4.4 OAuth proxy storage construction](https://github.com/PrefectHQ/fastmcp/blob/v3.4.4/fastmcp_slim/fastmcp/server/auth/oauth_proxy/proxy.py)
- [FastMCP v3.4.4 `OIDCProxy`](https://github.com/PrefectHQ/fastmcp/blob/v3.4.4/fastmcp_slim/fastmcp/server/auth/oidc_proxy.py)
- [FastAPI OAuth2/JWT authentication](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
- [Starlette middleware, including `SessionMiddleware`](https://www.starlette.io/middleware/#sessionmiddleware)
- [Starlette Jinja templates](https://www.starlette.io/templates/)
- [Authlib Starlette OAuth client integration](https://docs.authlib.org/en/latest/client/starlette.html)
- [Authlib authorization-server flow](https://docs.authlib.org/en/latest/flask/2/authorization-server.html)
- [RFC 7009 OAuth token revocation](https://www.rfc-editor.org/rfc/rfc7009)
- [RFC 7592 OAuth dynamic client registration management](https://www.rfc-editor.org/rfc/rfc7592)
- [RFC 8707 resource indicators for OAuth 2.0](https://www.rfc-editor.org/rfc/rfc8707)
- [Authentik DCR feature request, issue #8751](https://github.com/goauthentik/authentik/issues/8751)
- [Keycloak supported specifications](https://www.keycloak.org/securing-apps/specifications)
- [Keycloak client registration service](https://www.keycloak.org/securing-apps/client-registration)
- [Keycloak MCP authorization-server integration and conformance status](https://www.keycloak.org/securing-apps/mcp-authz-server)
- [Keycloak server extension development](https://www.keycloak.org/docs/latest/server_development/index.html)
- [Ory Hydra project and supported OAuth features](https://github.com/ory/hydra)
- [Ory Hydra custom login and consent flow](https://www.ory.com/docs/oauth2-oidc/custom-login-consent/flow)

The external-provider OAuth flow used by Airlock should also remain distinct from MCP URL elicitation for third-party authorization; see the [MCP elicitation specification](https://modelcontextprotocol.io/specification/draft/client/elicitation).
