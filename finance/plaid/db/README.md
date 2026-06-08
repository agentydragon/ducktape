# plaid_utils

Plaid client and sync utilities for personal accounts, cards, liabilities, and
investments. Backs the [Plaid Link management service](mcp_server/README.md).

Named `plaid_utils` (not `plaid`) so the top-level package doesn't collide with the
official `plaid` SDK — same convention as `openai_utils`.

`client.py` owns the official [`plaid-python`](https://github.com/plaid/plaid-python)
SDK client lifecycle. `PlaidClient` delegates the raw SDK calls used by sync,
adds app-level Link helpers, pins urllib3 to certifi's CA bundle, and supports
`with PlaidClient(...)` for short-lived jobs.

## Credentials

Stored SOPS-encrypted at `<../secrets/plaid.sops.yaml>` — single YAML with
`client_id`, `secrets.sandbox`, `secrets.production`. Decryptable by the 5
user-level age anchors (admin + wyrm2/rugged/atlas/iguana-agentydragon).

`plaid_utils.dev_creds.load()` reads it via `sops -d` and selects the secret by `$PLAID_ENV`.
Fallback: if the file is missing, it reads `PLAID_CLIENT_ID`/`PLAID_SECRET` from env. This
sops/env loader lives in `dev_creds.py`, separate from `client.py`, so the MCP server never
bundles it.

Plaid removed the `development` environment in 2024 — only `sandbox` (fake
banks, free, unlimited) and `production` (real banks, paid; first 10 Items
free on the Trial plan for teams created on/after 2026-04-15) remain.

## API warts

- **Amount sign:** positive = money **out** of the account (purchases, debits,
  card charges); negative = money **in** (payments, refunds, deposits). Same
  convention on credit and depository accounts.
- **`/transactions/get` vs `/transactions/sync`:** the MCP server uses
  `/transactions/get` for ad-hoc **date-range** reads — it natively takes
  `start_date`/`end_date` + `offset`/`count` and returns `total_transactions`.
  `/transactions/sync` is for stateful incremental mirroring (a cursor you must
  persist) and is the wrong primitive for range queries.
- **Cached vs live balances:** `/accounts/get` returns cached balances (Plaid
  refreshes 1–4×/day); `/accounts/balance/get` hits the bank live but is heavily
  rate-limited (5/min, 30/hour per Item).
- **Pending → posted:** a `pending` transaction is later replaced by a posted one
  whose `pending_transaction_id` points back to the pending id.
- **`ITEM_LOGIN_REQUIRED`:** when a bank login expires Plaid returns this error and
  the Item must be repaired through the Plaid MCP `/link` UI. The UI launches
  Plaid update mode for the existing Item.

## Sandbox smoke test

End-to-end, no Link UI: creates a fake public_token via `/sandbox/public_token/create`,
exchanges it for an access_token, pulls `/accounts/get` and `/transactions/sync`.

```bash
PLAID_ENV=sandbox bb run //plaid_utils:sandbox_smoke
```

## Real-account links

Managed at `https://plaid-mcp.allegedly.works/` or `/link`. The UI creates new Items,
requests additional consented products through Plaid update mode, repairs Items,
syncs Items, and removes Items. Access tokens are stored as Kubernetes Secrets in
the `plaid-mcp` namespace and are not written to Postgres.

All Plaid Link flows use a single Plaid OAuth redirect URI:
`https://plaid-mcp.allegedly.works/link/callback`. That URI must be allowlisted
in the Plaid developer dashboard for production Link to work.

## Rate limits

See <rate_limits.md>.
