# plaid_anomalies (example)

Financial transactions live in the Plaid Postgres mirror. Query it by launching a
`psql` pod in `haku-sandbox` (see `../AGENTS.md` → _Hard rules_; your home can't
reach the DB directly), using the read-only DSN from the `plaid-mcp-db-readonly`
secret. The role is read-only, so `psql` can only `SELECT`. `\dt` to orient on the
schema, then look at roughly the last 60 days for:

- duplicate charges (same merchant, amount, close dates)
- new recurring merchants (a subscription you may not know you have)
- recurring charges whose amount changed
- fees (overdraft, FX, card fees) — usually killable
- charges unusually large for their merchant's history

File one item per finding, evidence in `body` (date, merchant, amount, account).
Don't file expected regulars (rent, known subscriptions you've noted in `memory/`).
