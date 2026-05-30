# plaid TODO

- [ ] **Consider moving the Plaid MCP behind airlock.** Instead of (or in addition
      to) the standalone `mcp-oauth-facade` endpoint at `plaid-mcp.allegedly.works`,
      run `plaid-mcp-server` in the `airlock` namespace and mount it as an airlock
      backend (`backends.plaid` in `cluster/k8s/agents/airlock/config.yaml`). airlock
      already owns the Plaid tokens, so this would:
  - drop the cross-namespace reflector hop (server reads the token secrets in-namespace);
  - put every Plaid read behind airlock's existing Authentik auth + human-approval
    predicate (auto-approve the read-only tools, or require approval for sensitive ones).

  Tradeoff: reads would only be reachable through the airlock MCP endpoint and the
  approval flow, not as an independent server. Decide once the standalone facade has
  proven out.
