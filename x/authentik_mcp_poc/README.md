# authentik_mcp_poc

Proof of concept for a remote MCP server that authenticates its users through
Authentik, and then uses the resulting OAuth token to call a second service
that is **also** behind Authentik — a real Authentik **Proxy Provider outpost**,
not just JWT validation in the backend itself.

Two things are being demonstrated:

1. That FastMCP's `OIDCProxy` + Authentik implements the full **MCP remote
   authorization protocol** that claude.ai and Claude Code use for remote MCP
   servers with their own auth (OAuth 2.1 + PKCE, RFC 9728 protected-resource
   metadata, RFC 8414 AS metadata discovery, RFC 7591 dynamic client
   registration, RFC 8707 resource indicators).
2. That a single user identity flows through **two independent Authentik
   providers** — once when the MCP server validates the user's Authentik JWT,
   and again when the Authentik proxy outpost in front of the backend validates
   a **backend-scoped token** the tool handler mints on the fly via an
   RFC 7521 JWT-bearer client-credentials exchange (see <NOTES.md>). The
   `jwt_federation_providers` list on the backend proxy provider is the
   knob that authorizes the exchange. Net effect: one user login in
   claude.ai grants the agent the ability to call through a _real_ Authentik
   Proxy Provider outpost as that same user.

This is a toy: the only tool is `whoami_via_backend`, which forwards the
caller's Bearer JWT to the backend and returns the outpost-injected identity
headers. That's enough to prove every link of the chain is sound.

## Protocol quick reference

See also <../../docs/mcp_remote_auth.md> — the cluster-wide reference for how
claude.ai talks to remote MCP servers.

```
                         │ 0. user adds the MCP server URL to claude.ai
                         ▼
┌──────────┐  POST /mcp   ┌─────────────────┐
│ claude.ai│─────────────▶│ MCP server      │
│          │◀─────────────│ (FastMCP +      │  1. 401 + WWW-Authenticate
│          │    401       │  OIDCProxy)     │     resource_metadata=…
│          │              └────────┬────────┘
│          │                       │
│          │ 2. GET /.well-known/oauth-protected-resource
│          │──────────────────────▶│   (served by OIDCProxy)
│          │◀──────────────────────│   { authorization_servers: [MCP server itself] }
│          │                       │
│          │ 3. GET /.well-known/oauth-authorization-server
│          │──────────────────────▶│
│          │◀──────────────────────│   registration_endpoint, authorization_endpoint,
│          │                       │   token_endpoint — all on OIDCProxy
│          │                       │
│          │ 4. POST /register (RFC 7591 DCR)
│          │──────────────────────▶│
│          │◀──────────────────────│   { client_id: <ephemeral> }
│          │                       │
│          │ 5. browser → /authorize (PKCE, resource=MCP-server-URL)
│          │──────────────────────▶│
│          │                       │   (OIDCProxy redirects to Authentik)
│          │                       │
│          │                       │       ┌────────────────────────┐
│          │                       │──────▶│ Authentik              │
│          │                       │       │                        │
│          │                       │       │ authentik_provider_    │
│          │                       │       │   oauth2.mcp_poc       │
│          │                       │       │                        │
│          │◀──────────────────────┼───────│ 6. user consents       │
│          │ 7. redirect to OIDCProxy      │    → signed JWT        │
│          │    callback with code         └────────────────────────┘
│          │──────────────────────▶│
│          │◀──────────────────────│   token response: Authentik-signed JWT,
│          │                       │   passed through OIDCProxy unchanged
│          │                       │
│          │ 8. POST /mcp w/ Authorization: Bearer <fastmcp-jti>
│          │──────────────────────▶│   ┌───────────────────────────┐
│          │                       │──▶│ OAuthProxy.load_access_   │
│          │                       │   │ token swaps the JTI ref   │
│          │                       │   │ for the upstream          │
│          │                       │   │ Authentik user token.     │
│          │                       │   │ Tool runs.                │
│          │                       │   └─────────────┬─────────────┘
└──────────┘                                         │
                                                     │ 9a. token exchange
                                                     ▼
                            POST https://auth.allegedly.works/application/o/token/
                                grant_type            = client_credentials
                                client_id             = <proxy provider client_id>
                                client_assertion_type = …:jwt-bearer
                                client_assertion      = <user upstream token>
                                                     │
                                                     │ Authentik runs
                                                     │ __validate_jwt_from_provider
                                                     │ against mcp_poc_backend.
                                                     │ jwt_federation_providers
                                                     │ = [mcp_poc.id], mints a
                                                     │ NEW AccessToken bound to
                                                     │ the proxy provider with
                                                     │ user = original user.
                                                     ▼
                                            { access_token: <backend-scoped JWT> }
                                                     │
                                                     │ 9b. tool calls backend
                                                     ▼
                        GET https://authentik-mcp-poc-backend.allegedly.works/whoami
                        Authorization: Bearer <backend-scoped JWT>
                                   │
                                   ▼
                        ┌──────────────────────────┐
                        │ Authentik embedded       │
                        │ outpost                  │
                        │ (authentik-server:80)    │
                        │                          │
                        │ ─ matches Host header    │
                        │ ─ introspects token via  │
                        │   RFC 7662 (scoped to    │
                        │   proxy provider's own   │
                        │   client_id, so only     │
                        │   backend-scoped tokens  │
                        │   match)                 │
                        │ ─ checks application     │
                        │   policy bindings        │
                        │ ─ sets X-Authentik-*     │
                        └────────────┬─────────────┘
                                     │ internal_host
                                     ▼
                          ┌─────────────────────┐
                          │ whoami backend      │
                          │ (FastAPI)           │
                          │                     │
                          │ reads               │
                          │   X-Authentik-      │
                          │     {Username,      │
                          │      Email,         │
                          │      Groups, Uid}   │
                          │                     │
                          │ returns JSON echo   │
                          └─────────────────────┘
```

## Why JWT federation and not a proxy outpost in front of the MCP server?

The obvious alternative is to put the MCP server _itself_ behind an Authentik
Proxy Provider and skip OIDCProxy entirely — let the outpost do SSO. That
doesn't work for remote MCP:

- The MCP client (claude.ai, Claude Code) needs to drive OAuth itself — it
  expects RFC 7591 DCR, RFC 9728 metadata, and PKCE to its own redirect URI,
  not a forward-auth 302-to-login flow. The outpost speaks the browser-SSO
  dialect, not the RFC 7591 dialect.
- The proxy provider's internal OAuth2 client is locked to the redirect URI
  `<external_host>/outpost.goauthentik.io/callback`, so you can't repurpose it
  as the OIDCProxy upstream either.

And the obvious alternative on the backend side is to skip the outpost and
have the backend validate Authentik JWTs directly with `JWTVerifier`. That
works but doesn't demonstrate anything about the **forward-auth layer** — it's
just the MCP server's own auth pattern repeated twice.

JWT federation via a tool-side token exchange is the knob that lets both ends
be "real":

- MCP server uses OIDCProxy-wrapped `authentik_provider_oauth2`, so the OAuth
  flow is fully RFC-compliant.
- The tool handler exchanges the user's upstream token for a backend-scoped
  one via Authentik's `/application/o/token/` endpoint with
  `grant_type=client_credentials` + a JWT-bearer client assertion. The
  backend proxy provider's `jwt_federation_providers = [<oauth2 provider id>]`
  is the list of providers whose tokens Authentik will accept as assertions.
- Backend sits behind `authentik_provider_proxy`, and the outpost validates
  the (new, backend-scoped) Bearer via RFC 7662 introspection against its own
  provider — so the introspection lookup finds the token and lets the request
  through.

> **Two gotchas we hit during bringup, in case they come up again:**
>
> 1. `OIDCProxy` does NOT pass the upstream Authentik token to the MCP client
>    unchanged — it mints a FastMCP-signed JTI reference token. Reading the
>    raw `Authorization` header in a tool handler gives you that reference,
>    not the upstream token. Use `get_access_token().token` instead, which
>    returns the upstream token after `OAuthProxy.load_access_token`'s
>    server-side swap.
> 2. Authentik's proxy outpost does NOT consult `jwt_federation_providers`
>    when validating forward-auth Bearer headers. Its introspection is
>    scoped to the proxy provider's own `client_id`, so it only recognizes
>    tokens issued by THAT provider. `jwt_federation_providers` is only
>    consulted by the `/application/o/token/` endpoint, in the
>    `client_credentials` + JWT-bearer client-assertion path — i.e., you use
>    it to MINT a new, backend-scoped token at tool-call time. This is what
>    `_exchange_token_for_backend` in `server.py` does.
>
> Full forensic write-up, including source-level references in both FastMCP
> and Authentik, in <NOTES.md> §2-§5.

## Layout

| Path                                                                                | What it is                                                     |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `server.py`                                                                         | FastMCP server, `_build_auth`, `whoami_via_backend`            |
| `backend.py`                                                                        | FastAPI whoami backend, trusts `X-Authentik-*`                 |
| `config.py`                                                                         | Pydantic settings for both processes                           |
| `test_backend.py`                                                                   | Smoke test for the backend header contract                     |
| `BUILD.bazel`                                                                       | Two `oci_image` / `ghcr_push` pairs                            |
| `../../cluster/terraform/gitops/authentik-mcp-poc/`                                 | Both Authentik providers + K8s secret                          |
| `../../cluster/k8s/agents/authentik-mcp-poc/namespace/`                             | Layer 1: namespace-only Flux Kustomization                     |
| `../../cluster/k8s/agents/authentik-mcp-poc/tf/`                                    | Layer 2: Flux Terraform CRD wrapping the TF module             |
| `../../cluster/k8s/agents/authentik-mcp-poc/app/`                                   | Layer 3: Deployments, Services, HTTPRoute                      |
| `../../cluster/k8s/authentik/proxy-routes/authentik-mcp-poc-backend-httproute.yaml` | HTTPRoute in the `authentik` namespace pointing at the outpost |

The three-layer split keeps bootstrap ordering explicit:
`authentik-mcp-poc-namespace` → `authentik-mcp-poc-tf` → `authentik-mcp-poc`
(the `app/` Flux Kustomization). `tf/` writes the OIDC client-credentials
Secret into the already-existing namespace; `app/` consumes that Secret via
`secretKeyRef` in the server Deployment.

## Terraform: two providers, one JWT

```hcl
# User-login AS: OIDCProxy upstream.
resource "authentik_provider_oauth2" "mcp_poc" {
  client_type                = "confidential"
  allowed_redirect_uris      = [{
    matching_mode = "strict"
    url           = "https://authentik-mcp-poc.allegedly.works/mcp/auth/callback"
  }]
  # …
}

# Backend forward-auth: trusts JWTs from mcp_poc above.
resource "authentik_provider_proxy" "mcp_poc_backend" {
  external_host            = "https://authentik-mcp-poc-backend.allegedly.works"
  internal_host            = "http://authentik-mcp-poc-backend.authentik-mcp-poc.svc.cluster.local:8080"
  mode                     = "proxy"
  jwt_federation_providers = [authentik_provider_oauth2.mcp_poc.id]
  # …
}
```

The full module is at <../../cluster/terraform/gitops/authentik-mcp-poc/main.tf>.

## Adding it to claude.ai / Claude Code

Claude.ai → Settings → Connectors → Add remote MCP server:

- **Remote MCP server URL**: `https://authentik-mcp-poc.allegedly.works/mcp`
- **OAuth client ID**: leave blank (DCR)
- **OAuth client secret**: leave blank

Claude Code:

```bash
claude mcp add --transport http authentik-mcp-poc \
  https://authentik-mcp-poc.allegedly.works/mcp
```

Then `/mcp` → Connect → click through the Authentik consent screen → run the
`whoami_via_backend` tool. The response body contains your Authentik username
and email (from the outpost headers) plus a `secret_message` proving the
outpost let the request through.

## Building and pushing

```bash
bbr build //x/authentik_mcp_poc/...
bbr test  //x/authentik_mcp_poc/...

# Push to GHCR (CI does this automatically on merge to devel):
bb run //x/authentik_mcp_poc:server_push_ghcr
bb run //x/authentik_mcp_poc:backend_push_ghcr
```

## Verification in the cluster

After Flux has reconciled the three layers in order — `authentik-mcp-poc-namespace`
(just the namespace), `authentik-mcp-poc-tf` (Authentik providers + K8s secret),
and `authentik-mcp-poc` (the app Deployments) — you can poke at the running
stack with:

```bash
# 1. Terraform CRD applied, providers exist, secret written.
kubectl -n flux-system get terraform authentik-mcp-poc
kubectl -n authentik-mcp-poc get secret authentik-mcp-poc-oidc

# 2. Both Deployments ready.
kubectl -n authentik-mcp-poc get deploy

# 3. MCP server returns a 401 challenge with resource_metadata.
curl -i https://authentik-mcp-poc.allegedly.works/mcp

# 4. Backend is reachable through the outpost — expect an Authentik 302 when
#    unauthenticated (SSO redirect), not a 200 from the FastAPI app.
curl -i https://authentik-mcp-poc-backend.allegedly.works/whoami

# 5. End-to-end: add the MCP server to claude.ai or Claude Code, run the tool.
```

## Things this POC intentionally skips

- No request signing, no audience-split JWTs: everything uses a single
  `openid`+`email`+`profile` scope and the user's own identity.
- No NetworkPolicy on the backend — in production the backend port should
  only be reachable from the outpost, so direct in-cluster calls bypass the
  outpost. For the POC we rely on the hostname/outpost coupling.
- No scope-based RBAC in the MCP server. There's one tool and it runs for
  any authenticated user.
