# Plaid TODO

## Cross-ID mapping after relinking

The Link service can delete local data for a detached account after a preview and revalidation
check. Plaid may return different account IDs after relinking or account changes; the inventory
does not infer that two IDs represent the same real-world account or map history between them.

## Recoverable whole-Item removal

`/api/links/{item_id}/remove` removes the remote Item, deletes its access-token Secret, then purges
mirrored rows. A crash after Secret deletion leaves the local Item without a token to retry; a lost
`/item/remove` response also leaves remote progress uncertain. Track each explicit removal durably
across these steps and define how to reconcile an ambiguous Plaid response without assuming repeated
`/item/remove` calls are idempotent. Preserve the user's whole-Item removal scope and retain the
append-only `sync_runs`, `plaid_api_events`, and webhook audit rows.
