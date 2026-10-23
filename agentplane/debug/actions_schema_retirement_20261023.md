# Actions schema retirement

Read-only inventory on 2026-10-23, before preparing the next Goose migration. The inventory made no database changes and read no credential values. Source audit used `devel` at `3e6f13d635a271c07e9cd3b2acab1774d8a4c06f` and deployed image source `dedb568`.

## Deployed reader boundary

Staging Actions is Ready 2/2 and testing Actions Ready 1/1, both on `agentplane-action-service:devel-20261023094129-dedb568`. Their migrator image is `agentplane-action-service-migrate:devel-20261021220301-bdeae93`. Staging Goose versions 0 through 3 are applied.

The deployed Go grant reader still selects `external_connection_grant.activation_deadline`; the gRPC view and frontend adapter expose it. Keep that column until its reader-removal image has replaced the deployed readers. Go creates grants active without this deadline.

Neither current source nor the deployed Go source has a runtime reader/writer of `connection_enrollment.browser_hash`, `connection_enrollment.decision_digest`, `external_connection_grant.request_digest`, or `agentplane_oauth_kv`. The old Python runtime is retired. The grant integration fixture still explicitly inserts `request_digest` and must stop doing so with its column removal. Whole-tree searches included generic Postgres store and table-prefix configuration.

## Staging inventory

| Records         |                   Count |
| --------------- | ----------------------: |
| Action requests |                   4,597 |
| Connections     |                       1 |
| Grants          | 3 (1 active, 2 revoked) |
| Enrollments     |                       4 |
| Legacy OAuth KV |                   1,591 |

All three grants have the two legacy grant fields populated; all four enrollments have both legacy enrollment fields populated. The table drop therefore removes 1,591 old KV records, and the column drops remove stored legacy values; this is not an empty-schema cleanup. It preserves Connection/grant identities, current OAuth state, Action history and Sandbox data. These counts do not establish external-client reauthorization; that manual follow-up remains unverified.

Catalog inspection found no incoming foreign key to `agentplane_oauth_kv` and no constraint using the three immediately removable columns. Use a new migration without `CASCADE`; retain existing Goose SQL/history and current `oauth_*`, MCP linkage and upstream token tables. The legacy KV table needs `IF EXISTS` because fresh Goose databases never create it.

The live generic PostgreSQL OAuth storage helper in `mcp_infra/persistence.py` remains in use with the separate `mcp_oauth_kv` table. The retired Actions OAuth proxy had configured that helper with `table_name="agentplane_oauth_kv"`; no current caller does so. This migration leaves `mcp_oauth_kv` and its generic storage owner unchanged.

After merge and rollout, verify the actual migrator version, Actions readiness and preserved identity/history counts before burning down the rollout entry. No restoration rehearsal is required by the operator.
