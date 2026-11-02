# MCP edge legacy Actions schema retirement

The [staging investigation](../mcp_edge/debug/staging_handoff_20261023.md) found eight old
Connection/grant/enrollment records in Actions, with no matching IDs in the populated MCP edge.
The integrated Actions Connection/OAuth server was already removed. On 2026-11-01 PDT, the operator
approved deleting those eight records and all six old Actions Connection/external OAuth tables.
The earlier archive and data handoff proposals are superseded by this decision.

Actions migration 9 drops `oauth_token`, `oauth_authorization`, `oauth_registered_client`,
`connection_enrollment`, `external_connection_grant`, and `external_connection` in dependency order.
It does not use `CASCADE`, so any unexpected outside schema dependency stops the migration. It does
not connect to or change the MCP edge database. Actions' upstream OAuth linkage/token state,
Action history, policies, and Web Push remain owned by Actions.

The migration runs during normal Actions startup. The observed staging Actions Deployment was
Ready 2/2 on image `devel-20261102025119-ee4378d`; commit `ee4378d55f` descends from
[#12238](https://github.com/agentydragon/ducktape/pull/12238), which deleted the integrated
Connection/OAuth serving path. The current Action Service source has no runtime reader or writer of
these six tables. Recheck the deployed image and ancestry before rollout; this is the overlapping
old binary the migration must tolerate. Verify successful Goose version 9, Actions readiness,
and a normal MCP edge sign-in/tool call after Flux rollout. The MCP edge's live Connection and OAuth
rows must remain unchanged by this Actions migration.

Reverting the application image does not recreate deleted rows. If the migration or rollout fails,
forward repair the Actions schema or restore the Actions database from a pre-drop backup to an
isolated database and recover only the needed state. Never restore a whole old Actions or MCP edge
snapshot over new writes, and never restart the retired integrated Connection/OAuth writer.
