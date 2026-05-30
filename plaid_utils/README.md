# plaid_utils

Plaid client for personal bank accounts — pull transactions, balances, and
credit-card liabilities. Backs the [Plaid MCP server](mcp_server/README.md).

Named `plaid_utils` (not `plaid`) so the top-level package doesn't collide with the
official `plaid` SDK — same convention as `openai_utils`.

`client.py` is a thin wrapper over the official [`plaid-python`](https://github.com/plaid/plaid-python)
SDK. The SDK is synchronous (urllib3, no asyncio API), so the client is synchronous too —
FastMCP runs the MCP tool functions in a worker thread, so nothing blocks an event loop.
It exposes the read endpoints the MCP server needs (`/accounts/get`, `/accounts/balance/get`,
`/transactions/get`, `/liabilities/get`) plus the sandbox helpers the smoke test uses
(`/sandbox/public_token/create`, `/item/public_token/exchange`, `/transactions/sync`).
Responses are run through the SDK's `sanitize_for_serialization` and validated into the
typed models in `models.py` at the boundary — callers get typed objects, not raw dicts.

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
  the Item must be **re-linked via airlock** (airlock owns the Plaid Link flow and
  the access-token secrets).

## Sandbox smoke test

End-to-end, no Link UI: creates a fake public_token via `/sandbox/public_token/create`,
exchanges it for an access_token, pulls `/accounts/get` and `/transactions/sync`.

```bash
PLAID_ENV=sandbox bb run //plaid_utils:sandbox_smoke
```

## Real-account link

Done via airlock's Plaid Link UI (`https://airlock.allegedly.works`), which stores
the access tokens as k8s secrets. The MCP server consumes those tokens.

## Rate limits

See <rate_limits.md>.
