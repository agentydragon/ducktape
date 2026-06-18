# plaid_anomalies (example)

Financial transactions live in the Plaid Postgres mirror. Query it by launching a
`postgres`-image pod in `haku-sandbox` whose **command is your `psql` query**, then
read the rows from `kubectl logs` — `exec`/`port-forward` don't work through the API
gateway (see `../instructions.md` → _Hard rules_; your home can't reach the DB
directly). The DSN comes from the `plaid-mcp-db-readonly` secret as an env var, and
the role is read-only, so `psql` can only `SELECT`. Query `information_schema` (or
`\dt`) first to orient on the schema, then look at roughly the last 60 days for:

- duplicate charges (same merchant, amount, close dates)
- new recurring merchants (a subscription you may not know you have)
- recurring charges whose amount changed
- fees (overdraft, FX, card fees) — usually killable
- charges unusually large for their merchant's history

File one item per finding, evidence in `body` (date, merchant, amount, account).
Don't file expected regulars (rent, known subscriptions you've noted in `memory/`).
