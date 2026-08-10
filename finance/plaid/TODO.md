# plaid TODO

## Accounts deselected from an Item are never retired from the mirror

`apply_accounts` upserts every account `/accounts/get` returns and removes nothing, so an account
that stops being returned keeps its row, its last balance and its transactions forever. Only
removing the whole Item purges anything.

Hit on 2026-08-10. The BofA and Merrill Items had both been granted the same two accounts (checking
`4648` and CMA-Edge `1A12`), giving four `accounts` rows for two real accounts — Plaid mints
`account_id` per Item, so nothing joins them. Repairing each Item through Plaid update mode with
account selection fixed it at the source: each Item now returns exactly one account. The mirror
still held all four rows afterwards, with the two orphans frozen at their last-known balances, and
any aggregate that pairs an account with its most recent snapshot regardless of age still
double-counted $8.7M. Resolved by deleting and recreating both links, which is a sledgehammer — it
costs the full transaction history, and Plaid freezes `days_requested` at first link so the depth
cannot be re-requested later.

What it should do instead: record that a sync stopped seeing an account rather than silently
keeping it. A `detached_at` on `accounts`, set when an account is absent from `/accounts/get` for
an Item that otherwise synced successfully, with readers filtering on it. That preserves history
(the transactions stay queryable) while keeping it out of "what do I own now" aggregates.

Two things to get right:

- Absence must be distinguished from a failed or partial sync. Only stamp `detached_at` when
  `/accounts/get` succeeded for that Item, or a transient error silently retires live accounts.
- Un-detach on reappearance — re-selecting an account in update mode should revive the row rather
  than leave it filtered out.

Until then, anything summing balances or holdings should dedupe on `(name, mask)` rather than
trusting `account_id` to be one row per real account. The augur budget read model and the
tender-proceeds tracking both read these tables.
