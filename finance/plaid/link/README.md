# plaid_utils/mcp_server

Plaid link-management runtime package.

The deployed image now runs [`app.py`](app.py): a FastAPI web UI for Plaid Link
management plus a shared synchronous full-refresh sync engine. The agent-facing
read path is not this package; it is EnterpriseDB Pg Airman MCP pointed at the
synced Postgres database with a read-only role.

## Entrypoints

- `//plaid_utils/mcp_server:app_cli` / `server_image`: web UI on `:8080`.
- `//plaid_utils/mcp_server:sync_cli` / `sync_image`: CronJob entrypoint that
  refreshes every active link into Postgres.

## Configuration

The web and sync entrypoints use `PlaidWebSettings`:

- `PLAID_MCP_PLAID_ENV` — `sandbox` or `production`.
- `PLAID_MCP_CLIENT_ID` / `PLAID_MCP_CLIENT_SECRET` — Plaid app credentials.
- `DATABASE_URL` — writer Postgres URL, usually CNPG secret `plaid-mcp-db-app`.
- `PLAID_MCP_PUBLIC_BASE_URL` — public UI origin; defaults to
  `https://plaid-mcp.allegedly.works`.
- `PLAID_MCP_TRANSACTION_DAYS` / `PLAID_MCP_INVESTMENT_TRANSACTION_DAYS` —
  full-refresh windows.

Access tokens are one Kubernetes Secret per Plaid Item and are never stored in
Postgres. The web UI writes those Secrets; the sync job reads them.

## Link UI

`/` and `/link` both serve the management UI for active Plaid Items. It can:

- search institutions, show what the chosen one supports, and create an Item for the
  products it offers that this app can sync (all preselected);
- choose the Transactions history depth for new Items and show the recorded or
  observed history window for active Items;
- show active Items, requested/authorized/billed products, sync time, and Secret name;
- launch Plaid update mode to repair or renew an Item;
- widen an existing Item to every product its institution supports, then sync it;
- run a manual sync for one Item;
- remove an Item through `/item/remove`, delete its access-token Secret, and
  purge its mirrored link/account/transaction rows. Append-only `sync_runs` and
  `plaid_api_events` rows are retained for synchronization audit history.

Plaid fixes `transactions.days_requested` when Transactions is first added to an
Item. Existing Items cannot be expanded by sending a larger value later; the UI
therefore records the value for new links and shows an observed synced range for
inherited links whose original Link request was not logged.

The production sync path still uses date-window full refreshes:
`/transactions/get`, `/investments/transactions/get`, `/accounts/get`,
`/investments/holdings/get`, and `/liabilities/get`. The `links` table has
reserved cursor/status columns for future Plaid `/transactions/sync` work, but
the current CronJob leaves them null.

All new-link and update-mode flows use the same Plaid OAuth redirect URI:
`https://plaid-mcp.allegedly.works/link/callback`. Keep that allowlisted in the
Plaid developer dashboard.

## Deployment

GitOps manifests live under
[`cluster/k8s/agents/plaid-mcp/`](../../cluster/k8s/agents/plaid-mcp/README.md).
The human UI is `https://plaid-mcp.allegedly.works/link`; the read-only SQL MCP
is `https://plaid-db.allegedly.works/mcp`. The domain root
`https://plaid-mcp.allegedly.works/` serves the same UI for convenience.
