# Plaid TODO

## Account-specific history removal and cross-ID mapping

The Link inventory tracks detachment by exact Plaid account ID and retains transaction history. A
separate change would need to define account-specific history removal. Plaid may return different
account IDs after relinking or account changes; the inventory does not infer that two IDs represent
the same real-world account or map history between them.
