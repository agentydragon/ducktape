# Actions schema retirement

Read-only verification after the 2026-10-23 Flux rollout:

- Staging Actions is Ready 2/2; testing is Ready 1/1. Both use service image
  `devel-20261023111932-6e7b459` and migrator image `devel-20261023114557-d74d20e`.
- Goose migration 00005 is applied in both databases. `external_connection_grant.activation_deadline`
  is absent. All retired columns and the old Actions OAuth KV table have left the schema through
  new migrations; landed SQL was not edited.
- Staging retains 4,597 Action requests, one Connection and three grants, matching the pre-retirement
  inventory. No credential values were read. No database, Sandbox or volume was recreated.

Flux selected published images, committed their tags and reconciled the Deployments; these were not
manual image patches. Images that still read removed columns cannot run against this schema.
External-client reauthorization remains manual and unverified. The operator waived restoration testing.

The separate MCP edge database exists; its schema/data transfer and runtime cutover remain pending.
These schema-retirement checks do not establish an edge extraction or a successful external login.
