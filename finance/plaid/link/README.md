# Plaid Link management service

FastAPI web UI, signed webhook receiver and queued Item sync worker. The synced database is
queried through pgweb (Agentplane) and read-only in-cluster Postgres (Haku); there is no Plaid
MCP endpoint. Manifests: [`cluster/k8s/agents/plaid-mcp/`](../../../cluster/k8s/agents/plaid-mcp/README.md).

## Entrypoints

- `//finance/plaid/link:app_cli` / `server_image`: web UI on `:8080`, served at
  `https://plaid-mcp.allegedly.works/link` (and `/`).
- `//finance/plaid/sync:sync_cli_bin` / `sync_image`: daily CronJob refreshing every active link.

## Configuration

Both use `PlaidWebSettings`:

- `PLAID_MCP_PLAID_ENV` (`sandbox`|`production`), `PLAID_MCP_CLIENT_ID`,
  `PLAID_MCP_CLIENT_SECRET`.
- `DATABASE_URL`: writer URL, usually CNPG secret `plaid-mcp-db-app`.
- `PLAID_MCP_PUBLIC_BASE_URL`: UI origin, default `https://plaid-mcp.allegedly.works`.
- `PLAID_MCP_WEBHOOK_URL`: set on new Link tokens and updated on existing Items during sync;
  keep it aligned with the `/webhooks/plaid` route.
- `PLAID_MCP_TRANSACTION_DAYS`: history depth requested when Transactions is first linked.
- `PLAID_MCP_INVESTMENT_TRANSACTION_DAYS`: investment-transaction window for the daily sync.

Web only: `PLAID_MCP_OIDC_{ISSUER,CLIENT_ID,CLIENT_SECRET,SESSION_SECRET}` for Authentik login
and the signed session. Terraform stores them in the `authentik` namespace; the app's
ExternalSecret reads only that Secret into `plaid-mcp`. The Authentik application is restricted
to `authentik Admins`.

Access tokens are one Kubernetes Secret per Item, never in Postgres; the UI writes them, sync
reads them.

## Link UI

- Institution search; product checkboxes are always the full synced set, with products the
  institution lacks greyed out. Link tokens do **not** pin `institution_id`: the SDK model has
  the attribute but Plaid answers `INVALID_INSTITUTION` (it is a response field).
- Transactions history depth for new Items; recorded or observed history window for active ones.
  Plaid fixes `transactions.days_requested` when Transactions is first added, so existing Items
  cannot be widened; inherited links whose request was not logged show the observed range.
- Active Items with requested/authorized/billed products, sync time, Secret name; update mode to
  repair/renew; a widen button (shown only when the institution offers unauthorized products,
  and naming them); manual sync.
- Account inventory by Item from the persisted mirror.
- Detached accounts can have their account row, mirrored transaction/history rows, and
  balance/holding/liability snapshots deleted without removing the Item or its access-token Secret.
  The preview identifies the exact account and shows record counts and history dates; deletion is
  rejected if membership, mirrored data, or Item sync state changed before confirmation.
- `/item/remove` deletes the access-token Secret and purges mirrored link/account/transaction
  rows; append-only `sync_runs`, `plaid_api_events` and webhook audit rows are kept.

All Link flows use the OAuth redirect `https://plaid-mcp.allegedly.works/link/callback`, which
must stay allowlisted in the Plaid dashboard.

## Account inventory

Only a successful `/accounts/get` response with an explicit account list reconciles membership;
omitted IDs become detached, while missing or invalid account data leaves membership unchanged.
Legacy rows remain unknown until a complete listing succeeds. `accounts.updated_at` records the
last listing that returned that account, and a later listing restores membership only for that exact
Plaid account ID.

Balances are cached `/accounts/get` snapshots, not live balances. The UI separates the latest sync
result from the last successful sync. Detached accounts leave current cash and holdings projections,
but transaction and investment history remains stored and queryable until explicitly deleted from the
local mirror. Account deletion preserves the Item, its access-token Secret, Item sync cursor and queue,
shared security rows, and append-only sync/API/webhook audit records. API audit payloads may contain
multiple accounts, so the operation does not erase an account from those retained payloads.
Spend/Budget configuration is not rewritten, and a later sync can recreate the account if Plaid returns
its ID. Item removal keeps its existing whole-Item behavior.

### Products vs accounts

`products` is hard-required against **the accounts the user selects**, checked only after bank
consent: requesting `liabilities` on an account set with no loan or card fails inside the Plaid
iframe even at an institution offering liabilities. So the client sends one anchor product (the
earliest in `Product` declaration order: transactions, investments, liabilities — broadest
first) and the rest as `required_if_supported_products`, which never fails the flow.

## Webhooks and sync

`/webhooks/plaid` (same Gateway route as the UI, the only OIDC-exempt endpoint) verifies Plaid's
ES256 JWT and raw-body hash, then stores the full body and envelope metadata in
`plaid_webhook_deliveries` (public schema, readable by `plaid_ro`) before dispatch — inspect it
for handled, ignored and unrecognized deliveries, including fields the envelope model does not
know.

- Durably queued: `TRANSACTIONS / SYNC_UPDATES_AVAILABLE` (runs `/transactions/sync`), and
  `HOLDINGS`, `INVESTMENTS_TRANSACTIONS`, `LIABILITIES` `/ DEFAULT_UPDATE` (run the full Item
  sync). Duplicates coalesce per Item; other types are recorded, acknowledged and ignored.
- The cursor and all added/modified/removed transactions commit together after pagination; a
  mutation reported mid-pagination restarts from the saved cursor.
- Each sync mirror write and terminal run transition verifies the run still owns the Item while
  holding the Item's database row lock. Recovery uses that same lock, so a resumed run cannot
  commit another cursor, account observation, holding capture, investment transaction, liability
  snapshot, or success after its successor is admitted. API receipts serialize on that lock too:
  a late response is audited as `ok_superseded` or `error_superseded` according to Plaid's
  outcome, retaining its payload without invalidating a successor's holdings capture.
- This fence applies to upgraded writers. During an image rollout, an old web process or an
  already-running CronJob can still write after its two-hour run is recovered. Complete coverage
  begins only when the Deployment uses the fenced image and every Job created from an older
  CronJob template is terminal, with no retry Pod left to start. Inspect Job templates as well
  as active Pods: an old Job can retry with its original image after the CronJob pin changes. The
  `/item/webhook/update` API call is outside the database transaction; the rollout must keep the
  configured webhook URL the same across overlapping writers so a late call cannot restore an
  older URL.
- The daily CronJob is the missed-webhook backstop and also refreshes accounts, holdings,
  investment transactions (the only date-window read) and liabilities.
- Transactions webhooks are configured per Item; other products may differ.
