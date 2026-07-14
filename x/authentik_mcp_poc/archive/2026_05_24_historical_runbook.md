# authentik_mcp_poc — historical operational runbook

> This is the operational runbook for the POC as it ran before archival on
> 2026-05-24. It is no longer reconciled by Flux; the manifests and Tofu
> module are parked under `x/authentik_mcp_poc/cluster/` and
> `x/authentik_mcp_poc/tf/`. Treat the commands and cluster-state checks
> below as a record of how it worked, not as live operations — see
> <../README.md> to revive it.

## Required moving parts

Every component below has to line up for this POC to work. Missing or wrong on
any of them produces a specific, localized failure that's easy to mistake for
something else — each row lists what you get if it's broken.

| #   | Component                                             | Where                                                                           | What it does                                                                                                                                                                                                                                                                                                                                                                   | Failure mode if missing                                                               |
| --- | ----------------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| 1   | `authentik_provider_oauth2.mcp_poc`                   | <../tf/main.tf>                                                                 | User-login AS, wrapped by OIDCProxy. `issuer_mode=per_provider`, `client_type=confidential`, `allowed_redirect_uris=[https://authentik-mcp-poc.allegedly.works/auth/callback]`, property_mappings = openid+email+profile                                                                                                                                                       | OAuth flow in claude.ai fails at `/authorize`                                         |
| 2   | `authentik_provider_proxy.mcp_poc_backend`            | same `main.tf`                                                                  | Forward-auth for the backend. `mode=proxy`, `jwt_federation_providers=[mcp_poc.id]`, `access_token_validity=hours=24`                                                                                                                                                                                                                                                          | Tool call gets 302 (SSO redirect) from outpost because no provider backs the hostname |
| 3   | `authentik_application` × 2 + policy bindings         | same `main.tf`                                                                  | Each provider needs an Application with a policy binding (here: `authentik Admins` group) or the user can't access it                                                                                                                                                                                                                                                          | `__check_policy_access` fails on the outpost side, 403                                |
| 4   | `kubernetes_secret "mcp_poc_oidc"`                    | same `main.tf`                                                                  | Writes `client_id`, `client_secret` (for the OAuth2 provider) **and `backend_client_id`** (auto-generated on the proxy provider) into the `authentik-mcp-poc` namespace                                                                                                                                                                                                        | MCP server pod crash-loops on startup (missing required Pydantic field)               |
| 5   | `authentik_provider_proxy` ↔ embedded outpost binding | <../../../cluster/k8s/authentik/app/blueprints/embedded-outpost.yaml> `!Find`   | The embedded outpost's `providers` list must include the proxy provider by name, or the outpost doesn't know the hostname and returns 404                                                                                                                                                                                                                                      | `/whoami` returns a branded Authentik 404 page                                        |
| 6   | Embedded outpost HTTPRoute                            | <../cluster/authentik-mcp-poc-backend-httproute.yaml>                           | Gateway-level HTTPRoute that routes `authentik-mcp-poc-backend.allegedly.works` → `authentik-server:80` in the `authentik` namespace                                                                                                                                                                                                                                           | DNS resolves but Envoy has no route, returns 404 from the Gateway itself              |
| 7   | MCP server HTTPRoute                                  | <../cluster/agents/app/server-httproute.yaml>                                   | Plain route for `authentik-mcp-poc.allegedly.works` → `mcp-server:8765` in the POC namespace (this one is NOT behind the outpost)                                                                                                                                                                                                                                              | claude.ai can't reach `/mcp` at all                                                   |
| 8   | `server-deployment.yaml` env wiring                   | <../cluster/agents/app/server-deployment.yaml>                                  | Maps `client_id`/`client_secret`/`backend_client_id` from the TF-managed secret to `AUTHENTIK_MCP_POC_OIDC_*` env vars. Also `FASTMCP_HOME=/tmp/fastmcp` (OIDCProxy writes encrypted DCR state there) and `enableServiceLinks: false` (so K8s `<SVC>_PORT=tcp://...` env vars don't collide with Pydantic settings parsing)                                                    | Pod crash-loops on startup, see <../NOTES.md> gotcha list                             |
| 9   | `OIDCProxy` wrapping provider 1                       | <../../../mcp_infra/authentik_auth/provider.py> `build_authentik_auth`          | `base_url` must NOT include `/mcp` (we serve via `mcp.http_app(path="/mcp")` directly, not via FastAPI mount), or AS metadata and the resource URL end up wrong                                                                                                                                                                                                                | 401 with mismatched `resource_metadata` URL, claude.ai refuses to proceed             |
| 10  | `CurrentAccessToken().token` (not raw header)         | <../../../mcp_infra/authentik_auth/token_exchange.py> request-scoped dependency | OIDCProxy hands claude.ai a FastMCP-signed JTI reference; the upstream Authentik token lives in the server-side encrypted store and is exposed on `AccessToken.token` only after `OAuthProxy.load_access_token` swaps it in. Raw `Authorization` header = wrong token                                                                                                          | Outpost introspection says "token is not active"; see NOTES.md §2                     |
| 11  | RFC 7521 JWT-bearer token exchange                    | <../../../mcp_infra/authentik_auth/token_exchange.py> `AuthentikTokenExchanger` | POST `/application/o/token/` with `grant_type=client_credentials`, `client_id=<proxy provider client_id>`, `client_assertion_type=...:jwt-bearer`, `client_assertion=<user upstream token>`, `scope=openid email profile ak_proxy`. Authentik validates the assertion via `__validate_jwt_from_provider`, preserves the user, mints a NEW token scoped to the backend provider | Outpost introspection says "token is not active"; see NOTES.md §3-§4                  |
| 12  | `scope=openid email profile ak_proxy`                 | <../../../mcp_infra/authentik_auth/token_exchange.py> `EXCHANGE_SCOPES`         | Property mappings in Authentik are scope-gated. Without `ak_proxy` the proxy-outpost claim mapping doesn't fire and the outpost still authenticates the request but injects **empty** `X-Authentik-*` headers — the sneaky failure mode that gives you a 200 with a blank identity                                                                                             | 200 from `/whoami`, `user`/`email`/`groups` all blank; see NOTES.md §6                |

### Terraform shape

```hcl
# 1. User-login AS: OIDCProxy upstream.
resource "authentik_provider_oauth2" "mcp_poc" {
  name              = "authentik-mcp-poc"
  client_id         = "authentik-mcp-poc"
  client_type       = "confidential"
  issuer_mode       = "per_provider"
  include_claims_in_id_token = true

  property_mappings = [
    # openid + email + profile scope mappings
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [{
    matching_mode = "strict"
    # Matches OIDCProxy's default `/auth/callback` redirect path relative
    # to `base_url = https://authentik-mcp-poc.allegedly.works` (no `/mcp`
    # prefix — the MCP server is served by `mcp.http_app(path="/mcp")`
    # directly, not mounted under FastAPI).
    url = "https://authentik-mcp-poc.allegedly.works/auth/callback"
  }]
}

# 2. Backend forward-auth: the outpost's OAuth2 client will accept JWT
#    assertions from the mcp_poc provider above.
resource "authentik_provider_proxy" "mcp_poc_backend" {
  name                     = "authentik-mcp-poc-backend"
  external_host            = "https://authentik-mcp-poc-backend.allegedly.works"
  internal_host            = "http://authentik-mcp-poc-backend.authentik-mcp-poc.svc.cluster.local:8080"
  mode                     = "proxy"
  access_token_validity    = "hours=24"

  # THE KNOB. Per authentik/providers/oauth2/views/token.py, this list is
  # filtered by __validate_jwt_from_provider when the /token/ endpoint
  # receives a client_credentials request with a JWT client_assertion —
  # and the resulting new AccessToken is stored with provider=<this
  # proxy provider>, which is exactly what the outpost's introspection
  # looks up on the forward-auth hop.
  jwt_federation_providers = [authentik_provider_oauth2.mcp_poc.id]
}

# 3. K8s secret exposes BOTH providers' client_ids plus OAuth2 client_secret.
resource "kubernetes_secret" "mcp_poc_oidc" {
  metadata {
    name      = "authentik-mcp-poc-oidc"
    namespace = "authentik-mcp-poc"
  }
  data = {
    client_id         = authentik_provider_oauth2.mcp_poc.client_id
    client_secret     = authentik_provider_oauth2.mcp_poc.client_secret
    backend_client_id = authentik_provider_proxy.mcp_poc_backend.client_id
    # No backend client_secret: the JWT assertion authenticates the
    # /token/ call, so we don't need one (see NOTES.md §5).
  }
}
```

The full module is at <../tf/main.tf>.

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

# CI pushes to GHCR automatically on merge to devel via the
# .github/workflows/push-images.yml matrix. Each merge that touches
# x/authentik_mcp_poc/ updates ghcr.io/agentydragon/authentik-mcp-poc-{server,backend}.
# Pushes are deduped by content digest, so unchanged images are no-ops.
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
# Expect three data keys: client_id, client_secret, backend_client_id.

# 2. Both Deployments ready.
kubectl -n authentik-mcp-poc get deploy

# 3. MCP server returns a 401 challenge with resource_metadata.
curl -i https://authentik-mcp-poc.allegedly.works/mcp
# Expect: HTTP/1.1 401 Unauthorized
# WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"

# 4. Backend's `/whoami` is ONLY reachable via the outpost with a valid
#    backend-scoped Bearer. Direct unauthenticated GET returns the outpost's
#    HTML "Unauthenticated" page.
curl -i https://authentik-mcp-poc-backend.allegedly.works/whoami
# Expect: HTTP/1.1 401 Unauthorized + HTML body from outpost

# 5. End-to-end: add the MCP server to claude.ai or Claude Code, run the
#    `whoami_via_backend` tool. Expected response is the JSON blob at the
#    top of this README. Any variation (blank fields, 401, 404) maps to
#    exactly one of the twelve rows in the "Required moving parts" table.
```

### End-to-end test result

Run on 2026-04-13 against the live cluster after all twelve components
were in place:

```json
{
  "backend_status": 200,
  "backend_url": "https://authentik-mcp-poc-backend.allegedly.works/whoami",
  "backend_response": {
    "user": "agentydragon",
    "email": "agentydragon@gmail.com",
    "uid": "6bdf979edce7a934bf8a246005bcb38103fc12b9b682b11b1dcf46cbe4402b00",
    "groups": ["authentik Admins", "Grafana Admins"],
    "secret_message": "auth flowed through the Authentik proxy outpost"
  }
}
```

For the full forensic trace of how we got there (three wrong turns along
the way), see <../NOTES.md>.

## Things this POC intentionally skips

- **No request signing, no audience-split tokens per backend.** We exchange
  once per tool call for a single backend. A multi-backend fanout would need
  N exchanges, one per backend's `client_id`.
- **No NetworkPolicy on the backend.** In production the backend port should
  only be reachable from the outpost so direct in-cluster calls can't bypass
  forward-auth. For the POC we rely on the hostname/outpost coupling —
  anyone inside the cluster could still curl the Service directly.
- **No scope-based RBAC in the MCP server.** There's one tool and it runs
  for any authenticated user. The `authentik Admins` policy binding on the
  Authentik Application is the only gate.
- **No proxy-provider client_secret.** The JWT assertion IS the
  authentication for the client_credentials grant (see NOTES.md §5), so
  we don't need to ship the backend proxy provider's auto-generated
  client_secret into the MCP server pod.
