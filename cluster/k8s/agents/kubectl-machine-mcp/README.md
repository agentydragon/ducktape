# kubectl-machine-mcp

Public, unprivileged `containers/kubernetes-mcp-server` deployment in OAuth
**passthrough** mode: it forwards the caller's own Authentik JWT straight to
kube-apiserver instead of exchanging it for cluster credentials, so the
caller's own RBAC group (not this server) determines what it can do.

Exposed at `https://kubectl-machine-mcp.allegedly.works` so Anthropic-hosted
managed agents (e.g. Haku's cloud agent, `cluster/k8s/haku/cloud-agent-tf`)
can reach the cluster with a vault-injected `static_bearer` token — that
bearer token is the only secret involved; the pod itself holds no cluster
credentials (its ServiceAccount has no RBAC bindings).

## Layout

| Path         | Role                                                                              |
| ------------ | --------------------------------------------------------------------------------- |
| `namespace/` | `kubectl-machine-mcp` Namespace (own Flux kustomization, applied first)           |
| `app/`       | Deployment, Service, HTTPRoute, ServiceAccount, and the public config `ConfigMap` |

## How it works

- **Token validation**: `app/configmap.yaml` (`00-public.toml`) points the
  server at the `kubectl-sandbox-client-credentials` Authentik provider's
  OIDC discovery/JWKS (`authorization_url`, `oauth_audience`). Tokens minted
  by the `authentik-jwt-rotation` CronJob against that provider validate
  here; the caller's group claim in the token (e.g. `haku` →
  `oidc-ksbx-groups:haku`) drives kube-apiserver RBAC, not this server.
- **Passthrough, no token exchange**: `cluster_auth_mode = "passthrough"` —
  the caller's JWT is forwarded as-is to kube-apiserver. There's no STS/token
  exchange step, unlike `kubectl-sandbox-mcp`'s scoped-token flow.
- **Public ingress**: `app/httproute.yaml` is **not** behind the Authentik
  forward-auth outpost — the MCP server validates bearer tokens itself via
  `kubernetes-mcp-server`'s built-in OAuth2/OIDC support, since its callers
  (Anthropic-hosted agents) can't participate in an interactive SSO flow.
- **Unprivileged pod**: `app/serviceaccount.yaml` grants no RBAC — all
  authorization happens via the forwarded caller token, not this
  ServiceAccount.

## Dependencies

`app/flux-kustomization.yaml` depends on `gateway` (HTTPRoute needs
`cluster-gateway`) and `agent-machine-access-tf` (creates the
`kubectl-sandbox-client-credentials` Authentik provider this server
validates tokens against).

## Consumers

- `cluster/k8s/haku/cloud-agent-tf` — Haku's Anthropic-hosted managed agent
  (parked; see that directory's README).
- `haku/runtime/managed_agent/anthropic_hosted/` — the design docs for the
  managed-agent path that uses this MCP as its cluster-access tool.
