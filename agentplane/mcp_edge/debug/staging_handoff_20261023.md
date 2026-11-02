# Staging MCP edge handoff

## Later decision, 2026-11-01 PDT

The operator approved deletion of the eight legacy Actions records and all six old Actions
Connection/external OAuth tables. Actions migration 9 implements that decision. The live MCP edge
database and its distinct records remain the current owner. The investigation and preservation
recommendations below describe the state and decision boundary **before** this approval.

## Current finding, 2026-11-02 04:15–04:22 UTC

The external serving handoff happened; the old identities are absent from the observed edge state.
The eight legacy records remain in `actions`. Their IDs have no overlap with the corresponding records in `mcp_edge`. Do not run the empty-destination transfer
or drop the legacy schema on this evidence. No database, writer, route or grant was changed in
this investigation.

### Live inventory

Read-only `psql` queries through `agentplane-staging/postgres-3` compared `md5(id::text)` sets,
selected non-secret relationship fields and timestamps. Queries used `BEGIN READ ONLY` for the
relationship comparison. Counts are observations, not a consistent snapshot across both databases.
No credential values, token hashes or authorization payloads were printed.

| Table                       | Actions                | Edge            |
| --------------------------- | ---------------------- | --------------- |
| `external_connection`       | 1                      | 1               |
| `external_connection_grant` | 3: 1 active, 2 revoked | 1 active        |
| `connection_enrollment`     | 4, all expired         | 2, both expired |
| `oauth_registered_client`   | 0                      | 0               |
| `oauth_authorization`       | 0                      | 2               |
| `oauth_token`               | 0                      | 1,014           |

The edge token count advanced from 1,012 in the earlier same-session inventory; it is a live
destination, not an empty migration target. All source foreign keys involving these six tables
are internal to this set; the live constraint inventory found no external reference to them.
Actions' upstream `mcp_oauth_token_state` is a separate, live owner and is outside this handoff.

- Source Connection: created `2026-09-17 04:57:29.580920Z`, last updated
  `2026-10-11 04:22:31.642174Z`, version 6. Grants have revisions 1–3, created September 17,
  September 18 and October 11; revision 3 still says `active`.
- Edge Connection and its revision-1 grant: created `2026-10-23 21:40:35.140660Z`, Connection
  version 1. Its allowed enrollment was exchanged at the same instant. The other enrollment has
  no operator or decision recorded.
- Corresponding Connection, grant and enrollment primary-key sets are disjoint. Display name,
  bound caller JSON, original caller JSON, and client ID match. Caller JSON contains only
  `namespace` and `name`, **no UID**. This establishes the same named caller, not continuity of a
  Kubernetes ServiceAccount incarnation.
- Accepted enrollments have the same operator subject. The October 11 source enrollment and the
  accepted edge enrollment have the same operator issuer; the two September source enrollments
  have a different operator issuer. Raw operator identifiers were not published.
- Grant client ID is `https://claude.ai/oauth/mcp-oauth-client-metadata` in both databases.
  Source grant issuer is `https://agentplane-actions-staging.allegedly.works/`; edge grant issuer
  is `https://agentplane-actions-staging.allegedly.works` (no trailing slash). Do not normalize it
  as a migration convenience: [`ResolveGrant`](../connections/authorizations.go) compares exact
  issuer and client strings as well as active status, binding and the current caller label.

### Serving and authority

Both `actions` and `mcp-edge` Deployments were Ready 2/2. Their images were respectively
`agentplane-action-service:devel-20261102025119-ee4378d` and
`agentplane-mcp-edge:devel-20261102025103-ee4378d` in `git.allegedly.works/ducktape-ci`.
The live `mcp-edge-mcp` HTTPRoute sends `/mcp`, discovery, registration, authorize, token, revoke
and callback paths to `mcp-edge`. `actions-mcp` sends only `/oauth/client-metadata.json` to Actions.

The source grant's `active` value is retained history, not proof of exposed authority. Actions'
integrated serving path was deleted in [#12238](https://github.com/agentydragon/ducktape/pull/12238).
The edge's [`VerifyBearer`](../authserver/tokens.go) reads its own token/authorization tables, then
resolves the grant in its own database. The source has no OAuth rows, its IDs are absent from the
edge, and its issuer differs. Matching caller names does not make the two grants interchangeable.
No external login, token use, revocation or Action execution was attempted in this investigation.

### History and the unresolved boundary

- The October 23 preflight recorded the same 1 Connection, 3 grants and 4 enrollments in Actions,
  with no OAuth rows, while the edge was scaled to zero.
- [#11732](https://github.com/agentydragon/ducktape/pull/11732) initialized the edge before the
  routing change. The current edge Goose history records migration 1 at
  `2026-10-23 14:39:48.923792Z`.
- [#11734](https://github.com/agentydragon/ducktape/pull/11734) merged at `14:50:59Z`. Its body
  required a coordinated maintenance transfer and explicitly said the configuration PR did not
  execute it. The current edge Connection was created nearly seven hours later.
- #12238 explicitly reported configuration/readiness evidence, **not transfer integrity**.
- [#13190](https://github.com/agentydragon/ducktape/pull/13190) removed the completed compatibility
  settings node from the DAG; that node had separately reserved schema preservation/retention.
  Settings removal did not validate a data handoff.

The reviewed PRs, repository history and current rows contain no successful transfer record.
Current migration timestamps predate the route switch, so they do not establish a later database
reinitialization. A restored database could retain those timestamps; deleted rows would not appear
in this inventory. Thus **never transferred** versus **transferred, then removed/restored** remains
unresolved. Normal current Connection handlers contain no deletion path for these rows, but that
cannot rule out historical administrative operations.

Current PostgreSQL `log_statement` is `none` and `shared_preload_libraries` is empty; this says
nothing conclusive about October 23 settings. The listed CNPG Backup objects cover October 26
through November 2, after the transition. No historical backup was restored and object-store/WAL
contents were not inspected. A retained October 23 maintenance transcript, database audit or
recoverable pre/post-cutover snapshot would be needed to distinguish those histories. Do not
interpret missing reviewed evidence as proof that a transfer never ran.

## Existing transfer is inapplicable to this destination

The now-retired `mcp-edge-transfer` command copied six tables, retaining IDs and
all supported fields, inside one destination transaction. It requires recognized histories,
only expected destination relations, **an empty destination**, and stopped source/destination
writers. It fails on any nonempty destination table. It records no separate durable transfer receipt.
Its confirmation flag cannot make a merge into the live edge safe.

The old October 23 procedure required stopping writers, copying into an empty edge, comparing
fields, switching routes and retaining the source. That procedure's empty-destination precondition is now false.
Do not empty the edge, overwrite its new records, restart the legacy writer, replay consent or
Actions, or insert the old active grant into live authority tables to manufacture continuity.
Preserve both datasets while the historical owner and retention contract are decided. Any future
handoff must preserve original IDs, grant revisions/statuses, operator/issuer associations and
new edge writes, with a separately validated recovery path before source retirement.
