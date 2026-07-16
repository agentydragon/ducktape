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
