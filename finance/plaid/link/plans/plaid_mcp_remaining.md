# Plaid MCP Remaining Plan

_Updated 2026-05-31._

## Current State

v0 now has the Plaid Link management UI at `https://plaid-mcp.allegedly.works/` and `/link`, product-profile selection, access-token Secrets, CNPG Postgres, Plaid-shaped synced tables, append-only API/sync audit tables, the 12-hour full-refresh CronJob, and the read-only SQL MCP surface at `plaid-db.allegedly.works`.

The agent-facing read path is intentionally Postgres SQL only. There are no v0 bespoke Plaid MCP tools.

## Cutover From Airlock

Airlock no longer owns Plaid Link or Plaid token brokering. The Plaid client credentials are SOPS-managed in the `plaid-mcp` namespace, and Airlock's old Plaid provider stanzas, env vars, and access-token mirrors have been removed.

Completed validation:

- Existing Chase and Bank of America Items synced through the new database path.
- New Interactive Brokers and Wealthfront Items linked through the v0 UI and synced into Postgres.
- A manual CronJob run completed successfully with all four active links reporting non-null `last_synced_at`.
- Claude.ai authenticated successfully against `https://plaid-db.allegedly.works/mcp`.

## New Link Validation

For future new Items:

1. Link through the UI.
2. Confirm a Kubernetes access-token Secret and `links` row are created.
3. Run or wait for sync.
4. Verify SQL rows for accounts, holdings, securities, investment transactions where available, balances, and `plaid_api_events`.
5. Verify the UI scope-upgrade, sync, remove, and repair paths for any institution-specific behavior.

## v0 Hardening

- Add or run the sandbox smoke through the new exchange path: create a sandbox Item, sync it, and assert rows in every expected table.
- Keep tests focused on product-profile gating, full-refresh reconciliation, absent-in-window transaction removal, pending-to-posted replacement, investment transaction pagination, holding/liability snapshots, API event redaction, `sync_runs`, and same-Item sync locking.
- Keep Postgres comments useful for MCP discovery whenever schema changes.

## v1 Sync

Implement Plaid-recommended incremental behavior after v0 is stable.

Primary docs:

- <https://plaid.com/docs/transactions/sync-migration/>
- <https://plaid.com/docs/transactions/transactions-data/>
- <https://plaid.com/docs/transactions/webhooks/>
- <https://plaid.com/docs/api/products/investments/>
- <https://plaid.com/docs/api/products/liabilities/>
- <https://plaid.com/docs/api/accounts/>
- <https://plaid.com/docs/api/webhooks/webhook-verification/>

Transactions:

- Use `/transactions/sync`, not `/transactions/get`, for recurring updates.
- Keep one cursor per Item unless we deliberately introduce per-account cursors.
- Accumulate all pages before applying a delta; commit `next_cursor` only after data changes commit.
- On `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, discard the partial batch and restart from the cursor used for that loop.
- Treat pending transactions as provisional and posted transactions as mutable.

Webhooks:

- Configure the Plaid webhook URL in `/link/token/create`.
- Verify Plaid webhook JWTs through `/webhook_verification_key/get`.
- Use webhooks to enqueue sync; keep Cron as the missed-webhook backstop.

Investments and liabilities:

- Holdings are current-state snapshots from `/investments/holdings/get`.
- Investment transactions use date windows and `count`/`offset` pagination; refetch an overlapping recent window for recurring sync.
- Liabilities are current-state snapshots from `/liabilities/get`.

## Optional Bespoke MCP Tools

Only add non-DB operations if the SQL read path proves insufficient:

- `get_live_balance(link, account=None)` using `/accounts/balance/get`.
- `sync_link(link)` and `sync_all()` for manual sync triggers.

Do not add link add/remove/repair tools; those stay in the human `/link` UI.
