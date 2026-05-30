# plaid/mcp_server

Read-only MCP server over the owner's Plaid-linked bank accounts. Built on
[`//plaid:client`](../client.py) (typed Plaid client) and FastMCP. The server is
**auth-oblivious**: it speaks MCP over HTTP on `:8080` and a front proxy
(`mcp-oauth-facade`) handles Authentik OAuth. One server holds every configured
item's access token; every tool takes an `item` selector.

## Tools

- `list_items` → `list[ItemSummary]`. Call first — discovers `item` keys and each item's products.
- `list_accounts(item)` → `list[AccountOut]`. Cached balances (Plaid refreshes 1–4×/day).
- `list_transactions(item, start_date, end_date, account_id?, offset?, count?)` → `TransactionPage`.
  Date range + `offset`/`count` pagination; `total` is the full in-range count before slicing.
- `get_credit_card_liabilities(item)` → `list[CardLiabilityOut]`. Only for items with the
  `liabilities` product; `aprs` is often empty (issuer-dependent).
- `get_live_balance(item, account_id?)` → `list[AccountOut]`. Real-time; rate-limited
  5/min·30/hour per item — use sparingly.

Amount sign on transactions: **positive = money out** (charges/debits), **negative = money
in** (payments/refunds/deposits). See [`../README.md`](../README.md) for the full set of
Plaid API warts (`/transactions/get` vs `/sync`, pending→posted, `ITEM_LOGIN_REQUIRED` →
re-link via airlock).

### Example output

`list_transactions(item="chase", start_date="2026-05-01", end_date="2026-05-31", offset=0, count=50)`:

```json
{
  "total": 83,
  "transactions": [
    {
      "transaction_id": "yhKx…",
      "account_id": "3Wq…",
      "date": "2026-05-20",
      "amount": 42.17,
      "iso_currency_code": "USD",
      "name": "WHOLE FOODS",
      "merchant_name": "Whole Foods Market",
      "category": { "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES" },
      "pending": false,
      "pending_transaction_id": null
    }
  ]
}
```

## Configuration

Pydantic `BaseSettings`, env prefix `PLAID_MCP_`:

- `PLAID_MCP_PLAID_ENV` — `sandbox` or `production`.
- `PLAID_MCP_CLIENT_ID` / `PLAID_MCP_CLIENT_SECRET` — Plaid app credentials.
- `PLAID_MCP_ITEMS_META` — JSON `list[PlaidItem]`: `{key, institution, products, access_token_env}`.
- `PLAID_MCP_HOST` / `PLAID_MCP_PORT` — bind address (default `0.0.0.0:8080`).

Each item's access token is read from the env var named by its `access_token_env` (e.g.
`PLAID_CHASE_ACCESS_TOKEN`), so tokens stay in Secrets and metadata in a ConfigMap.

## Run locally

```bash
bb run //plaid/mcp_server:server_cli
```

## Deployment

Standalone public endpoint `plaid-mcp.allegedly.works` (Authentik-gated via the
`mcp-oauth-facade` sidecar). Manifests and deploy notes:
[`cluster/k8s/agents/plaid-mcp/`](../../cluster/k8s/agents/plaid-mcp/README.md). Image:
`ghcr.io/agentydragon/plaid-mcp-server` (built and pushed by CI).
