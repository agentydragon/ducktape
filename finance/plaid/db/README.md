# plaid_utils

Plaid client and sync utilities for personal accounts, cards, liabilities, and
investments. Backs the [Plaid Link management service](../link/README.md).

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
sops/env loader lives in `dev_creds.py`, separate from `client.py`, so the deployed Link and
sync entrypoints do not bundle developer-only SOPS/subprocess loading.

Plaid removed the `development` environment in 2024 — only `sandbox` (fake
banks, free, unlimited) and `production` (real banks, paid; first 10 Items
free on the Trial plan for teams created on/after 2026-04-15) remain.

## API warts

- **Amount sign:** positive = money **out** of the account (purchases, debits,
  card charges); negative = money **in** (payments, refunds, deposits). Same
  convention on credit and depository accounts.
- **`/transactions/get` vs `/transactions/sync`:** the mirror uses `/sync` with one durable
  cursor per Item; `/get` (date range + `offset`/`count`) only for an explicitly bounded query.
- **Cached vs live balances:** `/accounts/get` returns cached balances (Plaid
  refreshes 1–4×/day); `/accounts/balance/get` hits the bank live but is heavily
  rate-limited (5/min, 30/hour per Item).
- **Pending → posted:** a `pending` transaction is later replaced by a posted one
  whose `pending_transaction_id` points back to the pending id.
- **`ITEM_LOGIN_REQUIRED`:** an expired bank login; repair the Item via update mode in the Link
  UI.

## Spend snapshot invalidation

PostgreSQL listeners can wait for `plaid_spend_changed`; its payload is empty.
Storage emits it with successful sync completion, visible link status changes,
removal of a linked Item and its mirrored data, detached-account mirror deletion,
and successful complete `/accounts/get` observations. Account-list notifications
commit with membership and balance snapshots, even if a later product sync fails.
It is a transient wake-up, not a durable change stream. Consumers reload the full
current snapshot after a notification and on startup or reconnect.

## Sandbox smoke test

End-to-end, no Link UI: creates a fake public_token via `/sandbox/public_token/create`,
exchanges it for an access_token, pulls `/accounts/get` and `/transactions/sync`.

```bash
PLAID_ENV=sandbox bb run //finance/plaid/db:sandbox_smoke_bin
```

## Real-account links

Managed through the [Link management service](../link/README.md) (`ITEM_LOGIN_REQUIRED` repairs
included).

## Rate limits

See <rate_limits.md>.
