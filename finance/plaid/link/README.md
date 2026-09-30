# Plaid Link management service

This package provides the FastAPI web UI, signed webhook receiver, and queued Item
sync worker. The synced database is queried through pgweb by Agentplane and through
read-only in-cluster PostgreSQL access by Haku; there is no Plaid MCP endpoint.

## Entrypoints

- `//finance/plaid/link:app_cli` / `server_image`: web UI on `:8080`.
- `//finance/plaid/sync:sync_cli_bin` / `sync_image`: CronJob entrypoint that
  refreshes every active link into Postgres.

## Configuration

The web and sync entrypoints use `PlaidWebSettings`:

- `PLAID_MCP_PLAID_ENV` — `sandbox` or `production`.
- `PLAID_MCP_CLIENT_ID` / `PLAID_MCP_CLIENT_SECRET` — Plaid app credentials.
- `DATABASE_URL` — writer Postgres URL, usually CNPG secret `plaid-mcp-db-app`.
- `PLAID_MCP_PUBLIC_BASE_URL` — public UI origin; defaults to
  `https://plaid-mcp.allegedly.works`.
- `PLAID_MCP_WEBHOOK_URL` — public HTTPS URL for Plaid webhook delivery. The
  app sets it on new Link tokens and updates existing Items during sync.
- `PLAID_MCP_TRANSACTION_DAYS` — history depth requested when Transactions is
  first linked.
- `PLAID_MCP_INVESTMENT_TRANSACTION_DAYS` — date window for investment
  transaction snapshots during the daily sync.

The web process additionally uses `PLAID_MCP_OIDC_ISSUER`,
`PLAID_MCP_OIDC_CLIENT_ID`, `PLAID_MCP_OIDC_CLIENT_SECRET`, and
`PLAID_MCP_OIDC_SESSION_SECRET` for its Authentik login and signed browser
session. Terraform stores those credentials in the `authentik` namespace;
the app's ExternalSecret reads only that named Secret into `plaid-mcp`. The
sync process does not receive the OIDC values. Access to the Authentik
application remains restricted to the `authentik Admins` group.

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

The app sets the webhook URL when creating a new Item. For existing Items, the
next daily or manual sync calls `/item/webhook/update` if needed. The public
`/webhooks/plaid` endpoint verifies Plaid's ES256 JWT and raw-body hash, then
stores the complete authenticated request body and envelope metadata in the
`plaid_webhook_deliveries` table before dispatch. The public-schema table is
readable by `plaid_ro`; use it to inspect handled, ignored, and unrecognized
deliveries, including future fields that the current envelope model does not
recognize. For example:

```sql
SELECT received_at, webhook_type, webhook_code, item_id, disposition, raw_body
FROM plaid_webhook_deliveries
ORDER BY received_at DESC;
```

The audit row is retained when an Item is removed. The endpoint durably queues
`TRANSACTIONS / SYNC_UPDATES_AVAILABLE`, `HOLDINGS / DEFAULT_UPDATE`, and
`INVESTMENTS_TRANSACTIONS / DEFAULT_UPDATE`, and `LIABILITIES / DEFAULT_UPDATE`
events. The background worker coalesces duplicate notifications per Item.
Transaction events run `/transactions/sync`; Holdings, investment transaction,
and Liabilities events run the existing full Item sync so all selected product
snapshots refresh. Other webhook event types are recorded, acknowledged, and
ignored. The cursor and all added, modified, and removed transactions commit
together after pagination completes. If Plaid reports a mutation during
pagination, the sync restarts from the saved cursor.

The daily CronJob is the missed-webhook backstop. It also refreshes accounts,
holdings, investment transactions, and liabilities; only investment
transactions remain date-window reads. The mirror uses `/transactions/sync`
for transaction history instead of full-refresh `/transactions/get` calls.

All new-link and update-mode flows use the same Plaid OAuth redirect URI:
`https://plaid-mcp.allegedly.works/link/callback`. Keep that allowlisted in the
Plaid developer dashboard.

Plaid posts signed webhooks to
`https://plaid-mcp.allegedly.works/webhooks/plaid`, on the same Gateway route as
the Link UI. The app exempts only this endpoint from browser OIDC and verifies
Plaid's signature and raw-body hash before accepting events. Keep
`PLAID_MCP_WEBHOOK_URL` aligned with the route before deploying. Transactions
webhooks are configured per Item; other Plaid products may have different
webhook configuration requirements.

## Deployment

GitOps manifests live under
[`cluster/k8s/agents/plaid-mcp/`](../../cluster/k8s/agents/plaid-mcp/README.md).
The UI is `https://plaid-mcp.allegedly.works/link`; the app performs Authentik OIDC
login and owns its browser session. The domain root `https://plaid-mcp.allegedly.works/`
serves the same UI for convenience. Read-only SQL access is provided separately by
pgweb for Agentplane and by in-cluster PostgreSQL access for Haku; no MCP endpoint is published.
