# MCP + OAuth + Authentik: What We Learned

Notes from building an in-cluster MCP server (`kubectl-sandbox-mcp`) that
authenticates MCP clients via Authentik and scopes any caller —
including cluster admins — down to sandbox-level Kubernetes permissions.

## Goal

Let anyone (including a cluster admin) connect to an MCP server from
Claude.ai / Claude Code, authenticate via the Authentik consent screen,
and get Kubernetes access **scoped to the `kubectl-sandbox-users` group
only** — no privilege escalation, even if the authenticating user has
cluster-admin elsewhere.

## Final architecture (what we actually shipped)

```text
MCP client (Claude Code)
  │ 1. Connect to MCP server, get 401
  │ 2. Fetch /.well-known/oauth-authorization-server (proxied to Authentik)
  │ 3. OAuth authorization_code + PKCE flow (browser consent at Authentik)
  │    → Authentik issues access token with `groups=["kubectl-sandbox-users"]`
  │      HARDCODED via a custom scope mapping, regardless of user's real groups
  │ 4. Reconnect with Authorization: Bearer <token>
  ▼
kubectl-sandbox-mcp pod (kubernetes-mcp-server in passthrough mode)
  │ 5. Validates token via JWKS, extracts groups claim
  │ 6. Passes token through to kube-apiserver
  ▼
kube-apiserver
  │ 7. AuthenticationConfiguration validates token, maps groups claim
  │    to oidc-ksbx-groups:kubectl-sandbox-users
  │ 8. RBAC enforces sandbox-only permissions
```

**Key insight**: the scoping happens **at token-issue time in Authentik**,
not via any exchange or intermediary. The access token handed to the user
_already_ has only the sandbox group in it.

## The MCP OAuth flow (as the spec defines it)

The MCP spec expects:

1. Client connects to MCP server, gets 401 + `WWW-Authenticate: Bearer`
2. Client fetches `/.well-known/oauth-protected-resource` on the MCP server
3. Client fetches `/.well-known/oauth-authorization-server` on the OAuth server
4. Client does **Dynamic Client Registration (RFC 7591)** — POST to
   `registration_endpoint` to register itself as a fresh OAuth client
5. Client does OAuth `authorization_code` flow (browser consent)
6. Client exchanges code for access token

**Step 4 (DCR) is problematic with Authentik** — see "The DCR problem" below.

## Things that bit us, in order

### Hairpin + CiliumNetworkPolicy

- `auth.allegedly.works` DNS resolves to the VPS IPs running the Cilium
  Gateway with `hostNetwork`.
- When a pod sends traffic to those IPs, the packet hairpins through the
  Gateway on the same node. Routing itself works fine.
- **But**: Authentik has a CiliumNetworkPolicy that only allows ingress
  from specific namespaces (`authentik`, `flux-system`, `monitoring`, etc.).
  When the hairpinned traffic reaches Authentik, Cilium evaluates the CNP
  at pod-to-pod level and rejects it because the source namespace
  (`kubectl-sandbox-mcp`) isn't in `fromEndpoints`.
- **Fix**: add the MCP namespaces to `authentik-server-ingress` CNP.

### Detour: in-cluster Authentik URL

We first tried using `http://authentik-server.authentik.svc.cluster.local/...`
as the `authorization_url`. It bypassed hairpin routing, but the
`openid-configuration` response from Authentik embeds the configured
issuer URL (including authorize/token endpoints), which would be the
in-cluster URL — broken for external MCP clients. Reverted to the
external URL after fixing the CNP.

### kubernetes-mcp-server v0.0.60 well-known proxy bug

The server proxies `/.well-known/*` requests from the MCP client upstream
to Authentik. Authentik returns 404 HTML for
`/.well-known/oauth-authorization-server` (it only implements
`openid-configuration`). v0.0.60 blindly tried to JSON-decode the HTML
and returned 500 to the client.

**Fix on upstream main** (unreleased): 404 fallback that generates the
`oauth-authorization-server` metadata from `openid-configuration`.

**What we did**: built our own image from upstream commit
`8d2bb9b748ba77075a0305389c105f202d7e9751` via
`.github/workflows/container-images.yml`, pinned in
`third_party/kubernetes-mcp-server-pin.txt`. All `CLEANUP` markers
point back to this — switch to the upstream image when it releases.

### The DCR problem

**Authentik does NOT support Dynamic Client Registration** as of 2026-04.
See [goauthentik/authentik#8751](https://github.com/goauthentik/authentik/issues/8751)
— feature request, milestone 2026.8.0, not shipped. Authentik's
`openid-configuration` has no `registration_endpoint`.

Claude Code's MCP SDK refuses to proceed without DCR:

```text
SDK auth failed: Incompatible auth server: does not support dynamic client registration
```

**Workaround**: Claude Code supports pre-configured OAuth clients via
`--client-id` (+ optional `--client-secret`, `--callback-port`). The MCP
server config provides the client_id; Claude Code uses it directly
instead of registering dynamically.

### Detour: client secret distribution problem

We first set up the Authentik OAuth2 provider as `client_type =
confidential`, which meant every user had to supply a client_secret to
`claude mcp add --client-secret`. The secret is shared across all users,
making rotation awkward and exfiltration from any one user's config
compromises all.

**Analysis**: the client secret adds essentially zero real security in
MCP's model because:

- The callback URL is `localhost:<port>`. An attacker with the secret
  still can't receive the auth code — it goes to the victim's localhost.
- PKCE protects the auth code from interception.
- Authentik's redirect_uri allowlist blocks phishing to attacker-
  controlled URIs.

**Fix**: make the user-facing OAuth2 provider `client_type = public`.
PKCE is still enforced (it's required by the MCP SDK), but no secret is
needed. Users just pass `--client-id`.

### Token exchange doesn't work with Authentik either

**Original plan**: Use RFC 8693 token exchange. Caller authenticates
with their real groups; the pod exchanges that token via Authentik's
token endpoint for one scoped to `kubectl-sandbox-users`. Requires a
separate confidential "exchange client" in Authentik.

**kubernetes-mcp-server supports RFC 8693** via
`token_exchange_strategy = "rfc8693"`, sending
`grant_type=urn:ietf:params:oauth:grant-type:token-exchange` to Authentik.

**But Authentik doesn't implement that grant type.** Its
`grant_types_supported` is:

```text
[authorization_code, refresh_token, implicit, client_credentials,
 password, urn:ietf:params:oauth:grant-type:device_code]
```

No `token-exchange`. Result: `unsupported_grant_type` at tool-call time.

The existing grocy/airlock pattern uses a _different_ exchange:
`grant_type=client_credentials` with `client_assertion_type=jwt-bearer`
and the user's JWT as `client_assertion`. This is what FastMCP's
`AuthentikExchangeAuth` implements. But kubernetes-mcp-server doesn't
speak that dialect, and writing a FastMCP wrapper around it is a
significant rewrite.

## The real fix: scope at token-issue time

Instead of scoping _after_ Authentik hands out a token, scope it
_during_ token issue: use an Authentik **custom scope mapping** that
overrides the `groups` claim to a fixed `["kubectl-sandbox-users"]`
regardless of who the authenticating user is.

```hcl
resource "authentik_property_mapping_provider_scope" "kubectl_sandbox_fixed_groups" {
  name       = "kubectl-sandbox-mcp-fixed-groups"
  scope_name = "groups"
  expression = <<-EXPR
    return {"groups": ["kubectl-sandbox-users"]}
  EXPR
}

resource "authentik_provider_oauth2" "kubectl_sandbox_scoped" {
  # ...
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    authentik_property_mapping_provider_scope.kubectl_sandbox_fixed_groups.id,
  ]
}
```

The last mapping overrides the `groups` claim from `profile`. Even if an
admin user logs in, their issued token has `groups=["kubectl-sandbox-users"]`
and nothing else. Pass it through to kube-apiserver; sandbox-only RBAC
applies.

**This is dramatically simpler than the exchange approach**:

- No confidential exchange client
- No Kubernetes Secret
- No RFC 8693 dependency
- No proxy provider
- Pod runs in plain passthrough mode
- Single public (PKCE) OAuth2 provider in Authentik

## kubernetes-mcp-server config options we care about

- `require_oauth = true` — validate incoming Bearer tokens
- `authorization_url` — upstream OIDC issuer URL
- `oauth_audience` — expected `aud` claim
- `server_url` — public URL of this MCP server (in well-known responses)
- `cluster_auth_mode = passthrough` — forward caller's JWT to kube-apiserver
- `cluster_auth_mode = kubeconfig` — use pod's own SA (ignore caller token)
- `cluster_provider_strategy = in-cluster` — use in-cluster kubeconfig lookup
- `token_exchange_strategy = rfc8693` — (we don't use this; Authentik
  doesn't support the grant type)

## kube-apiserver AuthenticationConfiguration

The API server only validates JWTs from issuers declared in
`AuthenticationConfiguration` (see `cluster/terraform/main/infrastructure.tf`).
Each Authentik provider needs an entry: issuer URL + audiences +
claim mappings (prefix for username and groups).

Our prefix: `oidc-ksbx-groups:`. So a token claim
`groups=["kubectl-sandbox-users"]` becomes k8s group
`oidc-ksbx-groups:kubectl-sandbox-users`. Existing RoleBindings target
that prefixed group.

## Current state

Three kubectl MCP servers:

| Name                      | Transport             | Auth                                       | Permissions                                          |
| ------------------------- | --------------------- | ------------------------------------------ | ---------------------------------------------------- |
| `kubectl-local`           | stdio (local process) | client cert in kubeconfig                  | `kubectl-sandbox-users` group (via cert `O=` field)  |
| `kubectl-passthrough-mcp` | HTTP                  | OAuth passthrough (public client, PKCE)    | caller's own OIDC group permissions                  |
| `kubectl-sandbox-mcp`     | HTTP                  | OAuth passthrough + scope mapping override | always `kubectl-sandbox-users`, regardless of caller |

### `.mcp.json` for the scoped server

```json
{
  "mcpServers": {
    "kubectl-sandbox": {
      "type": "http",
      "url": "https://kubectl-sandbox-mcp.allegedly.works/mcp",
      "oauth": {
        "clientId": "kubectl-sandbox-mcp",
        "callbackPort": 8080
      }
    }
  }
}
```

No `client_secret`. No DCR. Just PKCE + pre-registered client_id.

Equivalent CLI:

```bash
claude mcp add --transport http kubectl-sandbox \
  https://kubectl-sandbox-mcp.allegedly.works/mcp \
  --client-id kubectl-sandbox-mcp \
  --callback-port 8080
```

### `claude.ai` web (Custom Connectors)

Claude.ai's hosted app also requires a pre-configured client_id since
Authentik doesn't do DCR. Its callback URL is fixed:
`https://claude.ai/api/mcp/auth_callback` — already added to the
provider's `allowed_redirect_uris`. In the Custom Connectors UI, paste:

- MCP server URL: `https://kubectl-sandbox-mcp.allegedly.works/mcp`
- Client ID: `kubectl-sandbox-mcp`
- Client Secret: (leave empty — public client, PKCE)

## Followups

- [ ] Add Gatus endpoint checks for MCP server health
- [ ] Watch for Authentik DCR release (2026.8.0?); once shipped, we can
      switch to DCR and remove the pre-configured client setup (though
      the current setup is arguably fine forever)
- [ ] Watch for kubernetes-mcp-server release with the well-known 404
      fallback; switch back to upstream image and delete our build job
      (see CLEANUP markers in `third_party/kubernetes-mcp-server-pin.txt`,
      `.github/workflows/container-images.yml`, and the deployment
      manifests)
- [ ] Consider a FastMCP wrapper pattern for future MCP servers that
      want DCR — `mcp_infra/authentik_auth/auth.py` provides
      `build_authentik_auth` / `AuthentikExchangeAuth` which implement
      DCR in-server plus Authentik-flavored JWT-bearer token exchange
