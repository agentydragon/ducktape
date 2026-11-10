# Plaid TODO

## Cross-ID mapping after relinking

The Link service can delete local data for a detached account after a preview and revalidation
check. Plaid may return different account IDs after relinking or account changes; the inventory
does not infer that two IDs represent the same real-world account or map history between them.
