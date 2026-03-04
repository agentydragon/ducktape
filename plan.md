# Plan: Tana MCP OAuth Token Broker Sidecar

## Completed Research

- [x] **tana-decomp PR**: Sent https://github.com/agentydragon/tana-decomp/pull/2
      correcting `~/.config/tana/` → `~/.config/Tana/` (capital T). The Tana binary
      calls `app.setName("Tana")` at startup, overriding the lowercase package.json
      name. This means `oauth-store.json` lives at `~/.config/Tana/oauth-store.json`
      — on the PVC mounted at `/home/tana/.config/Tana`.

- [x] **OAuth flow verified**: From the RE docs, the full OAuth 2.1 + PKCE flow
      is well-documented. Key facts:
  - `client_name: "Claude Code"` auto-approves (no modal needed)
  - Auth code TTL: 60s, Access token TTL: 4h, Refresh token: no expiry
  - The `/oauth/authorize` redirect is server-side HTTP 302 (no browser needed)
  - All OAuth endpoints are unauthenticated
  - The whole flow can be done with `curl` + a one-shot HTTP listener

- [x] **PVC covers oauth-store.json**: The PVC at `/home/tana/.config/Tana`
      covers the `oauth-store.json` path, so the sidecar could access it via shared
      volume if needed. But the API approach is cleaner.

## Goal

Add a sidecar to the tana-mcp pod that automatically obtains OAuth tokens from
Tana's MCP server and publishes them as K8s secrets (reflected to agent
namespaces). Cluster clients read the token from the secret and include it in
requests.

## Design

### Token flow

```
tana-desktop (127.0.0.1:8262)
    ^ OAuth flow (localhost, in-pod)
tana-token-broker sidecar
    | writes K8s Secret
tana-mcp namespace: tana-access-token
    | Reflector mirrors
claude-sandbox / openclaw-sandbox: tana-access-token
    | read by
MCP clients (include Authorization header in requests to tana-mcp svc:8263)
    | nginx rewrites Host/Origin
tana-desktop validates token, serves request
```

### Sidecar behavior

1. **Wait for Tana MCP**: Poll `GET http://127.0.0.1:8262/health` with
   exponential backoff (2s -> 4s -> 8s -> ... capped at 60s). This naturally
   handles the "human hasn't logged in yet" / "MCP not enabled yet" case.

2. **Register client**: `POST http://127.0.0.1:8262/oauth/register` with
   `client_name: "Claude Code"` and redirect URI
   `http://127.0.0.1:{CALLBACK_PORT}/callback`. Auto-approved by Tana because
   the name matches their allowlist.

3. **PKCE authorize**:
   - Generate `code_verifier` (128 chars) and `code_challenge` (S256).
   - Start a one-shot async HTTP server on `127.0.0.1:{CALLBACK_PORT}`.
   - `GET /oauth/authorize?...` with PKCE params.
   - Tana auto-approves and 302 redirects to our callback with `?code=ac_...`
   - Our listener catches the code and shuts down.

4. **Token exchange**: `POST /oauth/token` with
   `grant_type=authorization_code`, the auth code, and PKCE verifier. Get back
   `access_token` (4h TTL) + `refresh_token` (no expiry).

5. **Write K8s secrets**: Same pattern as oauth-broker:
   - `tana-tokens` (refresh secret): all fields, stays in `tana-mcp` namespace
   - `tana-access-token` (access secret): access_token + token_type +
     expires_at only, with Reflector annotations to mirror to
     `openclaw-sandbox,claude-sandbox`

6. **Refresh loop**: Sleep, wake up when token is within 1h of expiry (3h
   sleep for 4h tokens), call `POST /oauth/token` with
   `grant_type=refresh_token`, update both secrets.

7. **Retry on any failure**: If Tana goes down, token exchange fails, etc. --
   log the error and retry from step 1 with backoff.

### What about writing oauth-store.json directly?

The PVC at `/home/tana/.config/Tana` covers the oauth-store.json path, so we
_could_ pre-seed client registrations. But the `/oauth/register` endpoint is
unauthenticated and instant, so using the API is simpler and doesn't require
volume sharing between containers. We'd only need to write oauth-store.json
directly if we wanted to pre-populate grants (to skip the authorize step), but
the crypto makes that impractical.

### What cluster clients need to change

Clients currently hit `http://tana-mcp.tana-mcp.svc:8263/mcp` with no auth.
After this change they need to:

1. Read `tana-access-token` secret from their namespace
2. Include `Authorization: Bearer {access_token}` header

This is the same pattern as Google/Oura tokens from oauth-broker.

## Implementation Steps

### 1. Python package: `tana_token_broker/`

New package with:

- `broker.py` -- main logic (wait, register, PKCE, exchange, refresh loop)
- `cli.py` -- entrypoint (config from env vars, starts broker)
- `BUILD.bazel` -- py_library, py_binary, oci_image targets
- `test_broker.py` -- tests (mock Tana OAuth endpoints with respx)

Dependencies: `httpx`, `kubernetes-asyncio` (reuse `oauth_broker.k8s_client`).

### 2. Container image

OCI image built via Bazel (same pattern as oauth-broker). Pushed to
`registry.allegedly.works/tana-mcp/tana-token-broker`.

### 3. K8s manifests (cluster/k8s/tana-mcp/)

- **RBAC**: ServiceAccount `tana-token-broker` + Role with secret CRUD in
  `tana-mcp` namespace + RoleBinding.
- **Deployment update**: Add sidecar container referencing the new image.
  Mount the serviceaccount token. No volume sharing needed with tana-desktop
  (everything goes through HTTP on localhost).
- **Kustomization**: Add rbac.yaml to resources.

### 4. Update README

Document the new auth flow and how clients should use the token.
