# Plaid Spend web UI

React/Mantine client for the authenticated Plaid Spend web view. `//finance/plaid/spend/ui:bundle` uses the shared
`spa_bundle` macro; the Python service serves its `dist` under `/static` behind the session middleware. The tabs use
hash URLs: `/#/spending`, `/#/transactions`, and `/#/configuration`. The browser handles tab history; the server serves
only `/`. A signed-in browser can open these URLs directly; the existing sign-in flow returns to `/` after
authentication. Spending data comes from `/api/v1/view` and `/api/v1/events`; the read-only Transactions tab requests
`/api/v1/transactions?period=...` and the Configuration tab uses `/api/v1/configuration`. The API routes accept either
the browser's signed cookie or a desktop Bearer token. Configuration omits Plaid account IDs. The view and transaction
responses carry typed period reports and a separate forecast; the web types are generated from the Pydantic OpenAPI
schema. The Spending page's estimate-window control requests a 7-day or 30-day server forecast through both the view
and live-event routes. That one selection drives the balance estimate, pace warning, and purchase check; card statements
and historical spend totals keep their own periods. Transaction rows use short account display names and show counterparties, with named Plaid Transactions fields
in expandable details. The details can contain account and transaction IDs, so they are available only through the
authenticated route. The purchase estimate is advisory only. Verify layout in an authenticated browser after deployment
before calling visual regressions resolved.
