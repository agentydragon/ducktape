# Tana MCP Server

Runs the [Tana](https://tana.inc) desktop app in a Kubernetes container with an
nginx proxy that rewrites `Host`/`Origin` headers to `localhost` so Tana's MCP
server accepts requests from cluster clients.

## Purpose

This deployment exists to turn Tana's desktop-only local MCP server into a
cluster-consumable MCP endpoint without exposing Tana's OAuth approval flow to
other cluster clients.

For remote OAuth-capable clients, there is now a separate public facade
deployment. The internal `tana-mcp` service documented here remains the
bearer-authenticated downstream that the facade talks to with the server-held
PAT.

Tana normally exposes MCP only on loopback inside the desktop app process. This
deployment adds:

- a graphical desktop wrapper so an operator can do the one-time Tana login and
  enable the Local API/MCP server
- a cluster-internal proxy so workloads can reach `/mcp` even though Tana
  itself only listens on `localhost`
- a SOPS-managed Kubernetes secret containing a Tana personal access token for
  the full `agentydragon@gmail.com` account, which cluster clients can use to
  authenticate to `/mcp`

## Security Model

- `tana-desktop` listens on `127.0.0.1:8262` inside the pod
- the nginx sidecar on `8263` exposes only `/mcp` and `/health` to cluster
  clients and explicitly blocks `/oauth/*`

The proxy still blocks `/oauth/*` even though the current deployment no longer
uses the in-pod OAuth broker. Cluster clients only need `/mcp` and `/health`,
so exposing Tana's OAuth endpoints would just create unnecessary attack
surface.

## Architecture

- **tana-desktop container**: Ubuntu + Xvfb + noVNC + Openbox + Tana Desktop.
  Tana's MCP server listens on `localhost:8262` inside the pod.
- **proxy sidecar**: `nginx:alpine` rewrites `Host`/`Origin` to localhost before
  proxying only `/mcp` and `/health` to port 8262. Exposed on port 8263
  (cluster-internal only).
- **PVC**: Persists `~/.config/Tana` (login session, MCP client approvals).
- **PAT secret**: `tana-agentydragon-gmail-com-account-pat` is a
  SOPS-encrypted Kubernetes secret that stores a Tana personal access token for
  the full `agentydragon@gmail.com` account.

The old `token-broker` code is intentionally kept in the repo but is currently
unwired from this deployment because Tana's auto-approved OAuth callback flow
is no longer reliable enough to depend on operationally.

## Initial Setup (Graphical Login)

The Tana desktop app requires a one-time graphical login to your Tana account.
After login, the session persists in the PVC across pod restarts.

### Expected pre-login state

Before the one-time login is completed, the deployment intentionally looks
"half up":

- `kubectl -n tana-mcp get deploy,pods` shows the deployment at `0/1` and the
  pod at `1/2`

That is the expected state before the GUI login and MCP toggle are done.

### 1. Start from a fresh pod window

The `tana-desktop` container has a `startupProbe` on `/health`. Until login is
finished and the MCP server is enabled, Kubernetes restarts it roughly every 20
minutes. Start with a fresh restart window so you are not racing an old pod:

```bash
kubectl -n tana-mcp rollout restart deploy/tana-mcp
kubectl -n tana-mcp rollout status deploy/tana-mcp --timeout=2m
kubectl -n tana-mcp get pods -w
```

Wait until the new pod is `Running` and shows `1/2` ready.

### 2. Connect via noVNC

```bash
kubectl port-forward -n tana-mcp svc/tana-mcp 6080:6080
```

Open this exact URL in your browser:

- <http://localhost:6080/vnc.html?autoconnect=true&resize=scale>

Do not use bare <http://localhost:6080> as the operator entrypoint. That is
just the noVNC web root and may show an index page instead of attaching to the
desktop session.

### 3. Sign into Tana

- The Tana app shows its login screen on startup
- Sign in with your Tana account (email + password, or SSO)
- The login flow opens a separate browser window inside the same virtual
  desktop via `xdg-open`
- If the Tana window says `Logging in using browser...`, stay in noVNC and look
  for the browser window there rather than on your host desktop
- The desktop now runs a lightweight window manager, so you can move/focus the
  browser and Tana windows instead of interacting with a bare root window

### 4. Enable the MCP Server

- Open Tana Settings (gear icon, or Menu > Options)
- Navigate to **Tana Labs**
- Enable **"Local API/MCP server (Alpha)"**
- The MCP server starts on port 8262 inside the container

### 5. Confirm MCP readiness

Once the MCP server is healthy, the deployment should become ready on its own.
The authentication material for cluster clients is no longer minted by an
in-pod broker; it is supplied separately through a SOPS-managed PAT secret.

You can verify separately:

```bash
kubectl -n tana-mcp get deploy,pods
```

The deployment should move to `1/1` available and the pod to `2/2` ready.

### 6. Disconnect noVNC

Close the browser tab. The Tana app continues running headlessly. You only need
noVNC again if the session expires or for troubleshooting.

## Connecting MCP Clients

The MCP endpoint is cluster-internal only (no public ingress).

- **Endpoint**: `http://tana-mcp.tana-mcp.svc.cluster.local:8263/mcp`
- **Auth**: Read `token` from the
  `tana-agentydragon-gmail-com-account-pat` secret and include
  `Authorization: Bearer <token>` in requests. This is a personal access token
  for the full `agentydragon@gmail.com` account, not a narrowly scoped
  broker-managed OAuth token.
- **Exposed paths**: only `/mcp` and `/health`. Tana's `/oauth/*` endpoints are
  intentionally not reachable through the proxy.

## Public OAuth Facade

External clients that need MCP OAuth/DCR should use the separate public facade:

- **Public endpoint**: `https://tana-mcp-facade.allegedly.works/mcp`
- **Auth**: Authentik MCP OAuth
- **Authorization**: Authentik application access is restricted by the
  `tana-agentydragon-gmail-com-account-access` group, intended to contain only
  the `agentydragon` user.
- **Downstream auth**: the facade injects the server-held
  `tana-agentydragon-gmail-com-account-pat` token when calling internal
  `tana-mcp`

This split is intentional:

- internal `tana-mcp` stays simple and bearer-authenticated
- public `tana-mcp-facade` handles Authentik OAuth and caller allowlisting
- the Tana PAT never leaves Kubernetes

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

Expected path behavior through the proxy:

- `POST /mcp`: allowed
- `GET /health`: allowed
- `/oauth/*`: `403`
- anything else: `404`

## Troubleshooting

- **Deployment stays `0/1`, pod stays `1/2`**: This is normal until the Tana
  login is complete and **Tana Labs > Local API/MCP server (Alpha)** is turned
  on.
- **Pod restarts while you are logging in**: The `startupProbe` allows about 20
  minutes for the initial setup. Restart the deployment to get a fresh window,
  then reconnect via noVNC.
- **MCP health check failing**: Tana may not be running or MCP not enabled.
  Connect via noVNC to check.
- **Proxy returns `403` on `/oauth/*`**: This is intentional. The deployed
  service surface is only `/mcp` and `/health`.
- **`Logging in using browser...` but no browser appears**: The pod is likely
  running an older `tana-desktop` image without the browser-launching
  dependencies (`xdg-utils` + GUI browser), or a browser that still has its
  own sandbox enabled inside the container. Rebuild and redeploy the image,
  then retry the login flow from the `vnc.html` URL above.
- **PAT auth failing**: Verify the SOPS-managed secret decrypts correctly and
  that clients are using the `token` key from
  `tana-agentydragon-gmail-com-account-pat`.
- **Session expired**: Connect via noVNC and sign in again. The PVC preserves
  state across normal pod restarts but Tana may expire the session after
  prolonged inactivity.
- **Updating Tana version**: Change `TANA_VERSION` in
  `tana/mcp_server/Dockerfile` and push. CI rebuilds the image and Flux
  auto-deploys.

## Secrets

| Secret                                    | Key     | Source                                        |
| ----------------------------------------- | ------- | --------------------------------------------- |
| `tana-agentydragon-gmail-com-account-pat` | `token` | SOPS-managed PAT for `agentydragon@gmail.com` |
