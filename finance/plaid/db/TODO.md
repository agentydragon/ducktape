# plaid TODO

- [ ] **Implement Plaid v1 transaction sync.** Production currently uses the
      12-hour `plaid-mcp-sync` full-refresh CronJob. Live state checked
      2026-06-12 showed `sync_runs.mode = v0_full_refresh`, null
      `links.transactions_cursor`, and recent Plaid API events on
      `/transactions/get` rather than `/transactions/sync`. The v1 shape should:
  - use `/transactions/sync` with one persisted cursor per Item unless there is
    a reason to introduce per-account cursors;
  - accumulate all pages before applying a delta and commit `next_cursor` only
    after DB changes commit;
  - restart from the prior cursor on
    `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`;
  - configure Plaid webhooks in `/link/token/create`, verify webhook JWTs with
    `/webhook_verification_key/get`, and use Cron as the missed-webhook
    backstop.

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
