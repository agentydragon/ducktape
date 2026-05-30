# plaid-mcp

Standalone, Authentik-gated deployment of the [Plaid MCP server](../../../../plaid_utils/mcp_server/README.md)
at `https://plaid-mcp.allegedly.works`.

## Architecture

Same two-container facade pattern as `manifold-mcp`:

```text
claude.ai / Claude Code ──OAuth──▶ plaid-mcp.allegedly.works
                                    │  HTTPRoute → Service :8765
                                    ▼
  pod plaid-mcp:
    facade (mcp-oauth-facade, :8765)   ── OIDCProxy against Authentik (plaid-mcp app,
      │                                    restricted to agentydragon); OAuth state in valkey
      │ localhost:8080/mcp
      ▼
    plaid-mcp-server (:8080)           ── auth-oblivious; calls Plaid with the owner's
                                          client creds + per-item access tokens
```

`CiliumNetworkPolicy` allows only the gateway ingress entity to reach `:8765`; the
upstream `:8080` is reachable only in-pod via loopback.

## Secrets

The `plaid-mcp-server` container consumes three Secrets mirrored from the `airlock`
namespace into `plaid-mcp` by the ExternalSecrets in
[`app/external-secret.yaml`](app/external-secret.yaml) — ESO's Kubernetes provider via
the shared `kubernetes-airlock-secret-store` ClusterSecretStore. ESO re-syncs every
minute, so (unlike the previous emberstack-reflector setup) no source-side annotations
or re-link dance are needed — each Secret appears here as soon as airlock holds it:

- `plaid-client-credentials` (`client_id`, `client_secret`) — airlock SOPS secret, the Plaid app creds.
- `plaid-chase-access-token` (`access_token`) — written by airlock at Plaid Link time.
- `plaid-bofa-access-token` (`access_token`) — written by airlock at Plaid Link time.

`plaid-mcp-oidc` (facade Authentik `client_id`/`client_secret`) is created by the
`agent-machine-access` Terraform module.

## Prerequisite: link the accounts in airlock

The two access-token Secrets only exist once each item has been linked through airlock's
Plaid Link UI (`https://airlock.allegedly.works`) at least once — Plaid access tokens are
minted at Link time and never auto-refresh. `plaid-client-credentials` is already present
(airlock deploys it from SOPS). Until both items are linked, ESO has nothing to mirror for
the missing token and the `plaid-mcp` pod stays pending on it (the Flux kustomization's
health check won't go ready).

## Verification

```bash
kubectl -n plaid-mcp get secret plaid-client-credentials plaid-chase-access-token plaid-bofa-access-token plaid-mcp-oidc
kubectl -n plaid-mcp get deploy plaid-mcp
curl -i https://plaid-mcp.allegedly.works/mcp   # expect 401 + WWW-Authenticate: Bearer resource_metadata=...
# Then add it in Claude Code / claude.ai, consent via Authentik, and call list_items.
```
