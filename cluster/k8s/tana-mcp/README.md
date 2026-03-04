# Tana MCP Server

Runs the [Tana](https://tana.inc) desktop app in a Kubernetes container with an
nginx proxy that rewrites `Host`/`Origin` headers to `localhost` so Tana's MCP
server accepts requests from cluster clients.

## Architecture

- **tana-desktop container**: Ubuntu + Xvfb + noVNC + Tana Desktop. Tana's MCP
  server listens on `localhost:8262` inside the pod.
- **proxy sidecar**: `nginx:alpine` rewrites `Host`/`Origin` to localhost before
  proxying to port 8262. Exposed on port 8263 (cluster-internal only).
- **token-broker sidecar**: Performs OAuth 2.1 + PKCE flow against Tana's MCP
  server, obtains access/refresh tokens, and writes them to the
  `tana-mcp-oauth-tokens` K8s secret. Refreshes automatically before expiry.
- **PVC**: Persists `~/.config/Tana` (login session, MCP client approvals).

## Initial Setup (Graphical Login)

The Tana desktop app requires a one-time graphical login to your Tana account.
After login, the session persists in the PVC across pod restarts.

### 1. Connect via noVNC

```bash
kubectl port-forward -n tana-mcp svc/tana-mcp 6080:6080
```

Open <http://localhost:6080> in your browser. You'll see the Tana desktop app
running in a virtual display.

### 2. Sign into Tana

- The Tana app shows its login screen on startup
- Sign in with your Tana account (email + password, or SSO)
- The login flow may open a browser window inside the virtual desktop — this is
  expected, the embedded Chromium handles it

### 3. Enable the MCP Server

- Open Tana Settings (gear icon, or Menu > Options)
- Navigate to **Tana Labs**
- Enable **"Local API/MCP server (Alpha)"**
- The MCP server starts on port 8262 inside the container

### 4. Approve MCP Client Access

- When the first MCP client connects, Tana shows an approval modal
- Approve the connection via the noVNC session
- Subsequent connections from the same client are auto-approved

### 5. Disconnect noVNC

Close the browser tab. The Tana app continues running headlessly. You only need
noVNC again if the session expires or for troubleshooting.

## Connecting MCP Clients

The MCP endpoint is cluster-internal only (no public ingress).

- **Endpoint**: `http://tana-mcp.tana-mcp.svc.cluster.local:8263/mcp`
- **Auth**: The token-broker sidecar obtains OAuth tokens automatically. Clients
  can read the `tana-mcp-oauth-tokens` secret for the access token and include
  `Authorization: Bearer <token>` in requests.

### Health Check

```bash
kubectl exec -n tana-mcp deploy/tana-mcp -c proxy -- \
  curl -s http://localhost:8263/health
```

### Port Forward for Local Testing

```bash
kubectl port-forward -n tana-mcp svc/tana-mcp 8263:8263
curl http://localhost:8263/health
```

## Troubleshooting

- **Pod stuck in startup**: The `startupProbe` allows 20 minutes for initial
  login. Connect via noVNC and complete the setup flow.
- **MCP health check failing**: Tana may not be running or MCP not enabled.
  Connect via noVNC to check.
- **Session expired**: Connect via noVNC and sign in again. The PVC preserves
  state across normal pod restarts but Tana may expire the session after
  prolonged inactivity.
- **Updating Tana version**: Change `TANA_VERSION` in
  `docker/tana-mcp/Dockerfile` and push. CI rebuilds the image and Flux
  auto-deploys.

## Secrets

| Secret                  | Key                 | Source                               |
| ----------------------- | ------------------- | ------------------------------------ |
| `harbor-ci-robot`       | `.dockerconfigjson` | Vault `kv/harbor/ci-robot`           |
| `tana-mcp-oauth-tokens` | `access_token`      | Auto-managed by token-broker sidecar |
| `tana-mcp-oauth-tokens` | `refresh_token`     | Auto-managed by token-broker sidecar |
