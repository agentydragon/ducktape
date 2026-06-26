# Plaid

Financial transactions live in the Plaid Postgres mirror (cluster-internal — your home
can't reach it). Query it by `kubectl apply`-ing a short-lived `postgres`-image Pod in
`haku-sandbox` (`restartPolicy: Never`) whose **command is your `psql` query**, then read
the rows from `kubectl logs` and delete the pod. Prefer command + logs over `exec`/
`port-forward`. Pull the DSN from the `plaid-mcp-db-readonly` secret as an env var via
`secretKeyRef` (so the credential never lands on a command line); `kubectl run --env`
can't pull from a secret, hence the manifest. The role is read-only, so `psql` can only
`SELECT` — no MCP server.

Schema:
[`finance/plaid/db/migrations/versions/0001_initial.py`](https://github.com/agentydragon/ducktape/blob/devel/finance/plaid/db/migrations/versions/0001_initial.py).
**Query the `current_transactions` view by default** (it excludes removed rows); columns
include `date, name, amount, merchant_name, account_id, pfc_primary, pfc_detailed`.
Query `information_schema` (or `\dt`) first if you need to orient. Look at roughly the
last 60 days for:

- duplicate charges (same merchant, amount, close dates)
- new recurring merchants (a subscription you may not know you have)
- recurring charges whose amount changed
- fees (overdraft, FX, card fees) — usually killable
- charges unusually large for their merchant's history

File one item per finding, evidence in `body` (date, merchant, amount, account).
Don't file expected regulars (rent, known subscriptions you've noted in `memory/`).
