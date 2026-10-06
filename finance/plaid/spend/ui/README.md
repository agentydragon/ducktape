# Plaid Spend web UI

React/Mantine client for the authenticated Plaid Spend web view. `//finance/plaid/spend/ui:bundle` uses the shared `spa_bundle` macro; the Python service serves its `dist` under `/static` behind the session middleware. Spending data comes from `/api/v1/web/view` and `/api/v1/web/events`; the read-only Configuration tab uses `/api/v1/web/configuration`, which omits Plaid account IDs. The purchase estimate is advisory only. Verify layout in an authenticated browser after deployment before calling visual regressions resolved.
