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

- search institutions; the product checkboxes are always the full set this app syncs, with the
  ones the chosen institution doesn't offer greyed out and unchecked (clearing the box re-enables
  everything). Link tokens deliberately do **not** pin `institution_id` — the generated SDK model
  carries that attribute but Plaid answers `INVALID_INSTITUTION`, since it is a response field, not
  a request one. Only one product goes in `products`; see _Products vs accounts_ below;
- choose the Transactions history depth for new Items and show the recorded or
  observed history window for active Items;
- show active Items, requested/authorized/billed products, sync time, and Secret name;
- launch Plaid update mode to repair or renew an Item;
- widen an existing Item to the products its institution offers that it isn't authorized for
  yet — the button appears only when there is something to add, and names it;
- run a manual sync for one Item;
- remove an Item through `/item/remove`, delete its access-token Secret, and
  purge its mirrored link/account/transaction rows. Append-only `sync_runs` and
  `plaid_api_events` rows are retained for synchronization audit history.

### Products vs accounts

Institution support and account support are different gates, and only the second one is checked
after the user has already authenticated at their bank. `products` is hard-required against **the
accounts the user selects** — so requesting `liabilities` at a brokerage that offers them
institution-wide, on an account set with no loan or card, fails Link _after_ bank-side consent with
a "no liability accounts" error inside the Plaid iframe.

The client therefore anchors on a single product and sends the rest as
`required_if_supported_products`, which activates per selected account and never fails the flow.
The anchor is the earliest in `Product` declaration order — transactions, then investments, then
liabilities — which is also broadest-to-narrowest, so the one product that _can_ fail is the one
least likely to.

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
