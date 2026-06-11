# MCP Remote Server Authentication

How Claude.ai (and other MCP clients) authenticate to remote MCP servers.

## Protocol Stack

The MCP Authorization Specification builds on OAuth 2.1 Authorization Code with PKCE,
layering several RFCs:

| RFC                                    | Role                                                                  |
| -------------------------------------- | --------------------------------------------------------------------- |
| OAuth 2.1 (`draft-ietf-oauth-v2-1-13`) | Base authorization framework                                          |
| RFC 7636 (PKCE)                        | Proof Key for Code Exchange — required for all clients                |
| RFC 9728                               | Protected Resource Metadata — server advertises its auth server       |
| RFC 8414                               | Authorization Server Metadata discovery                               |
| RFC 7591                               | Dynamic Client Registration                                           |
| RFC 8707                               | Resource Indicators — tokens are audience-bound to the MCP server URL |

## Configuration Fields

When adding a remote MCP server in Claude.ai, three fields are presented:

- **Remote MCP server URL**: The MCP server's HTTP endpoint. Also used to derive the
  authorization base URL (path stripped) for metadata discovery.
- **OAuth client ID** (optional): A pre-registered `client_id` for the server's
  authorization server. When omitted, the client uses Dynamic Client Registration
  (RFC 7591) to obtain one automatically, or connects without auth if the server
  doesn't challenge.
- **OAuth client secret** (optional): For confidential (server-side) clients. When
  omitted, the client acts as a public client relying solely on PKCE. When provided,
  it's sent during token exchange as additional client identity proof.

## Token Exchange Flow

```
MCP Client                MCP Server              Auth Server           User
    |                         |                        |                  |
    |--- unauthenticated ---->|                        |                  |
    |<-- 401 Unauthorized ----|                        |                  |
    |    WWW-Authenticate:    |                        |                  |
    |    resource_metadata=.. |                        |                  |
    |                         |                        |                  |
    |--- GET resource_metadata (RFC 9728) ------------>|                  |
    |<-- { authorization_servers: [...] } -------------|                  |
    |                         |                        |                  |
    |--- GET .well-known/oauth-authorization-server -->|                  |
    |<-- { authorization_endpoint, token_endpoint } ---|                  |
    |                         |                        |                  |
    | [if no client_id: Dynamic Client Registration (RFC 7591)]          |
    |--- POST /register ------------------------------>|                  |
    |<-- { client_id, client_secret? } ----------------|                  |
    |                         |                        |                  |
    | [generate PKCE code_verifier + code_challenge]   |                  |
    |                         |                        |                  |
    |--- redirect to /authorize (code_challenge, resource param) ------->|
    |                         |                        |--- consent ----->|
    |                         |                        |<-- grant --------|
    |<-- redirect to callback with authorization code -|                  |
    |                         |                        |                  |
    |--- POST /token (code + code_verifier + resource) |                  |
    |<-- { access_token, refresh_token } --------------|                  |
    |                         |                        |                  |
    |--- MCP request + Authorization: Bearer token --->|                  |
    |<-- MCP response --------|                        |                  |
```

### Step by Step

1. **Initial request**: Client sends an unauthenticated request to the MCP server URL.
2. **401 challenge**: Server responds with `HTTP 401` and a `WWW-Authenticate` header
   containing a `resource_metadata` URL.
3. **Protected Resource Metadata** (RFC 9728): Client fetches the metadata document to
   discover which authorization server(s) to use and what scopes are supported.
4. **Authorization Server Metadata** (RFC 8414): Client fetches
   `/.well-known/oauth-authorization-server` from the auth server. Fallback: if 404,
   use default paths (`/authorize`, `/token`, `/register`).
5. **Dynamic Client Registration** (RFC 7591): If the client has no `client_id` for
   this server, it POSTs to the registration endpoint to obtain one.
6. **PKCE generation**: Client generates a random `code_verifier` and computes the
   `code_challenge` from it.
7. **User authorization**: Browser opens the authorization endpoint. The user sees a
   **consent screen**, logs in, and grants permissions. The `resource` parameter
   (RFC 8707) binds the eventual token to this specific MCP server.
8. **Callback**: Auth server redirects to the client's callback URL with an
   authorization code (Claude.ai uses `https://claude.ai/api/mcp/auth_callback`).
9. **Token exchange**: Client sends the authorization code + `code_verifier` +
   `resource` to the token endpoint. Receives `access_token` and optionally
   `refresh_token`.
10. **Authenticated requests**: All subsequent MCP HTTP requests include
    `Authorization: Bearer <token>`.

## Step-Up Authorization

If the client receives `HTTP 403 Forbidden` with `error="insufficient_scope"` during a
tool call, it:

1. Parses required scopes from the `WWW-Authenticate` header.
2. Computes the union of previously granted scopes + newly required scopes.
3. Re-initiates the authorization flow with the combined scope set (user sees the
   consent screen again).
4. Retries the original request with the new token.

## Three Client Registration Methods

Tried in priority order:

1. **Client ID Metadata Documents** (preferred): The client hosts its own HTTPS URL as
   the `client_id` (e.g., `https://claude.ai/oauth/client-metadata.json`). The auth
   server fetches this to learn about the client without prior registration.
2. **Pre-registered credentials** (manual): User supplies a `client_id` and optionally
   `client_secret` obtained through the server's admin interface. This is what the
   optional fields in Claude.ai's UI are for.
3. **Dynamic Client Registration** (RFC 7591): Client POSTs to the registration
   endpoint and receives fresh credentials automatically.

## Client Types

- **Public client** (no `client_secret`): Relies on PKCE alone. Recommended for
  browser-based and desktop clients. This is the default when `client_secret` is
  omitted.
- **Confidential client** (with `client_secret`): Sends the secret during token
  exchange as additional proof. Used for server-side integrations.

## Other Grant Types

The MCP spec also defines extensions beyond the core Authorization Code flow:

- **Client Credentials** (`io.modelcontextprotocol/oauth-client-credentials`):
  Machine-to-machine auth with no user interaction or consent screen. Uses either
  JWT Bearer Assertions (RFC 7523) or plain `client_id`/`client_secret`. Not exposed
  through Claude.ai's UI.
- **Enterprise-Managed Authorization**
  (`io.modelcontextprotocol/enterprise-managed-authorization`): Corporate SSO via an
  enterprise IdP (Okta, Azure AD). User authenticates once via SSO; the IdP issues an
  ID-JAG (Identity Assertion JWT Authorization Grant) that the client exchanges for an
  MCP access token. No per-server consent — access is managed centrally by IT.

## Security Requirements

- PKCE is **required** for all clients (not optional).
- All authorization endpoints must be served over HTTPS.
- Redirect URIs must be localhost or HTTPS URLs.
- Tokens must not appear in URI query strings.
- Tokens are audience-bound to the specific MCP server URL via RFC 8707 resource
  indicators — a token for server A cannot be used against server B.
- Servers must validate redirect URIs to prevent open redirects.
