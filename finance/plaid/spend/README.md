# Plaid Spend

`//finance/plaid/spend:server_image` serves the same backend-computed statement-cycle view to every
authenticated desktop client. The card selection, labels, limits, and alert thresholds live in the
SOPS-managed `plaid-spend-cards` Secret under `cluster/k8s/agents/plaid-mcp/spend/`.

## API

- `GET /api/v1/view` returns the configured enabled card aggregates. Money values are integer minor
  currency units; no transactions or Plaid credentials are returned.
- `GET /api/v1/events` sends an immediate `view` SSE event, then recomputes and sends a complete
  view after `plaid_spend_changed` notifications. Reconnects compute a new initial view; idle
  streams send comments as heartbeats.
- Both endpoints require a Bearer access token from the Authentik `plaid-spend-desktop` OIDC client.
  Configuration is global to the service, so every authorized identity sees the same view.

The view shape is:

```json
{
  "generated_at": "2026-09-30T12:00:00Z",
  "cards": [
    {
      "account_id": "plaid-account-id",
      "label": "Everyday card",
      "account_name": "Plaid account name",
      "institution_name": "Bank name",
      "mask": "1234",
      "currency": "USD",
      "cycle_start": "2026-09-01",
      "spend_minor_units": 34218,
      "posted_minor_units": 30000,
      "pending_minor_units": 4218,
      "limit_minor_units": 100000,
      "alert_threshold_percent": 80,
      "spend_percent": 34.218,
      "alert_state": "normal",
      "last_synced_at": "2026-09-30T11:58:00Z",
      "statement_available": true
    }
  ]
}
```

Null cycle totals indicate no statement date; `institution_name` and `mask` may be null. Minor units
follow the currency's ISO precision (for example, USD cents and JPY whole yen).

The statement cycle starts the day after the latest credit-liability snapshot's
`last_statement_issue_date`. A missing date leaves cycle and spend values null and marks the card
unavailable; the service does not substitute calendar-month bounds. Posted and pending amounts are
summed separately, pending rows superseded by a posted transaction are dropped, removed rows and
credit-card payment transactions are excluded, and transactions with an explicit different currency
are not included. Refunds retain Plaid's negative sign. Spend percentage uses the configured limit;
the alert becomes `warning` at its threshold and `exceeded` at 100 percent. Card views identify both
the Plaid account name and its institution when available.

## Shared card configuration

Edit the encrypted Secret with `sops cluster/k8s/agents/plaid-mcp/spend/cards.sops.yaml`. Its
`stringData.cards.json` value has this shape:

```json
{
  "cards": [
    {
      "account_id": "plaid-account-id",
      "label": "Everyday card",
      "limit_minor_units": 100000,
      "alert_threshold_percent": 80,
      "enabled": true
    }
  ]
}
```

Use `null` for an optional limit or threshold. The service validates this file at startup and reads
it from `/etc/plaid-spend/cards.json`. A Secret update rolls the Deployment, so the next view and
SSE event use the new configuration. The spend database role can only read the four Plaid source
tables needed to compute the view; it does not store or write this configuration.

## Runtime settings

All service settings use the `PLAID_SPEND_` prefix except `DATABASE_URL`:

- `DATABASE_URL`
- `PLAID_SPEND_CARDS_CONFIG_PATH` (default `/etc/plaid-spend/cards.json`)
- `PLAID_SPEND_API_OIDC_ISSUER`, `PLAID_SPEND_API_OIDC_CLIENT_ID`,
  `PLAID_SPEND_API_OIDC_DISCOVERED_ISSUER`, `PLAID_SPEND_API_OIDC_JWKS_URI`, and optional
  comma-separated `PLAID_SPEND_API_OIDC_SIGNING_ALGORITHMS` (default `RS256`)
- optional `PLAID_SPEND_HOST` and `PLAID_SPEND_PORT`

The API resolver verifies Authentik RS256 access tokens against pinned issuer/JWKS metadata and
requires the configured audience and authorized party. The listener subscribes to committed
`plaid_spend_changed` notifications from the separate Plaid sync service.
