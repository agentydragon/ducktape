# plaid-mcp

Standalone, Authentik-gated deployment of the [Plaid MCP server](../../../../plaid/mcp_server/README.md)
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

The `plaid-mcp-server` container consumes three Secrets that are **not** in this
kustomization — they arrive in the `plaid-mcp` namespace by reflection from the
`airlock` namespace (emberstack reflector):

- `plaid-client-credentials` (`client_id`, `client_secret`) — airlock SOPS secret, the Plaid app creds.
- `plaid-chase-access-token` (`access_token`) — written by airlock at Plaid Link time.
- `plaid-bofa-access-token` (`access_token`) — written by airlock at Plaid Link time.

`plaid-mcp-oidc` (facade Authentik `client_id`/`client_secret`) is created by the
`agent-machine-access` Terraform module.

## One-time operator steps

Two source-side reflector changes can't be fully automated from this repo and must be
applied once by an operator with the cluster age key:

1. **Reflect `plaid-client-credentials` to `plaid-mcp`.** Add the four
   `reflector.v1.k8s.emberstack.com/reflection-{allowed,allowed-namespaces,auto-enabled,auto-namespaces}`
   annotations (with `plaid-mcp` as the namespace) to
   `cluster/k8s/agents/airlock/plaid-client-credentials.sops.yaml` **via `sops`** (a plain
   text edit breaks the file — sops's MAC covers unencrypted metadata, so it must be
   re-encrypted; running `sops <file>` and editing does this).
2. **Re-link Chase and BofA in airlock** (`https://airlock.allegedly.works`). The
   `plaid-mcp` reflection target was added to both access secrets in airlock's
   `config.yaml`, but airlock only (re)writes those secrets when it writes a token — and
   Plaid access tokens never auto-refresh. Re-linking each item rewrites its
   `plaid-{chase,bofa}-access-token` secret with the updated reflection list, after which
   the reflector copies it into `plaid-mcp`.

Until both are done the `plaid-mcp` pod stays pending on the missing Secrets (the Flux
kustomization's health check won't go ready).

## Verification

```bash
kubectl -n plaid-mcp get secret plaid-client-credentials plaid-chase-access-token plaid-bofa-access-token plaid-mcp-oidc
kubectl -n plaid-mcp get deploy plaid-mcp
curl -i https://plaid-mcp.allegedly.works/mcp   # expect 401 + WWW-Authenticate: Bearer resource_metadata=...
# Then add it in Claude Code / claude.ai, consent via Authentik, and call list_items.
```
