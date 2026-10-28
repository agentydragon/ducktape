# plaid_utils

Plaid client and sync utilities for personal accounts, cards, liabilities, and
investments. Backs the [Plaid Link management service](../link/README.md).

Named `plaid_utils` (not `plaid`) so the top-level package doesn't collide with the
official `plaid` SDK — same convention as `openai_utils`.

`client.py` owns the official [`plaid-python`](https://github.com/plaid/plaid-python)
SDK client lifecycle. `PlaidClient` exposes the generated SDK as `client.sdk` for
sync's raw-response parsing, adds app-level Link helpers, pins urllib3 to certifi's CA bundle, and supports
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

## Numeric values

The sync path reads the generated SDK's raw HTTP response and parses JSON decimals directly into
`Decimal` before validating Plaid payloads; the SDK's normal response decoder would turn them into
binary floats first. Financial columns use PostgreSQL `NUMERIC` and SQLAlchemy `Numeric(asdecimal=True)`;
the application keeps balances, transaction amounts, holding quantities, prices, cost basis and values,
and investment-transaction values as `Decimal`. API-event JSON represents these decimals as strings,
and migration backfills cast each stored column's retained text representation to `NUMERIC` without
currency-scale rounding.

## Spend snapshot invalidation

PostgreSQL listeners can wait for `plaid_spend_changed`; its payload is empty.
Storage emits it with successful sync completion, visible link status changes,
removal of a linked Item and its mirrored data, detached-account mirror deletion,
and successful complete `/accounts/get` and holdings captures. Observation notifications
commit with membership and snapshots, even if a later product sync fails.
It is a transient wake-up, not a durable change stream. Consumers reload the full
current snapshot after a notification and on startup or reconnect.

## Holdings captures

Each successful `/investments/holdings/get` observation commits one `holding_captures`
row, its explicit `holding_capture_accounts` coverage and balance observations, and
the linked `holding_snapshots` rows in one transaction. The capture ID groups holdings;
timestamps are observation metadata, not batch identity. Readers choose the newest
capture for each Item, then require coverage for each requested account. They never
fall back to an older capture when the newest one omits an account. An explicitly
covered capture with no holdings means the account currently has no positions.

`read_current_holdings` returns only rows from the newest Item capture, so positions
that disappear from a complete observation disappear from the projection too. An
account with no capture or missing coverage is an error, distinct from an explicitly
covered empty capture. During rollout, readers reject unlinked snapshot rows
(`capture_id IS NULL`) at or after the newest complete capture and a later successful
`/investments/holdings/get` API receipt that has no linked capture. The receipt check
also catches older writers that return a successful empty response without appending
any rows. A later complete capture supersedes older unlinked rows and receipts; the
orphan-row timestamp only bounds the rollout guard and never matches a position to a
capture.

Migration `0010` matches each retained historical batch to exactly one successful
API receipt using its Item, sync window and complete retained holding JSON. It also
recovers covered accounts and balances from that receipt. A batch applied before a
later endpoint failed is retained; deleted Items and accounts are not recreated.
Missing or ambiguous matches abort the migration transaction. Empty legacy
responses are recoverable only when the overall sync succeeded, since the old
writer left no row proving that an empty response was applied.

The migration keeps old columns for existing processes and accepts legacy writers'
null capture IDs during rollout. Schema downgrade is rejected: converting back to
floats and dropping capture membership would lose exact values and historical
coverage. A rollback requires a corrective forward migration. Startup migrations
serialize with a transaction advisory lock and roll back on validation failure.

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
