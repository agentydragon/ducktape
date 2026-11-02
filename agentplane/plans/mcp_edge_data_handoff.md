# Retain MCP edge extraction history before source retirement

## Decision: historical owner and retention

The [read-only investigation](../mcp_edge/debug/staging_handoff_20261023.md) found eight legacy
records still in Actions, no matching IDs in the live edge, and no verified transfer record.
The current edge is already accepting writes. The ordinary empty-destination transfer is
inapplicable; the legacy `active` grant is not a live authority to reactivate.

**Proposed for operator review:** move an exact historical copy of the six legacy tables into a
separate archive schema owned with the MCP edge database. Keep it outside Connection/OAuth serving
queries and the serving role's writable objects. Preserve all source values, including the
`active` status, revoked revisions, issuer spelling and expired enrollments; archive membership
marks them as historical. Do not change the live edge IDs, grants or tokens. Preserve the original
source until field equality, restore and retention gates pass.

Alternative: retain the six tables in Actions under explicit historical ownership indefinitely,
with backup/restore coverage and a named reader/recovery procedure; this postpones schema retirement.
A backup-only archive needs an explicit retention period and demonstrated recovery before it can
replace the tables. Import into live Connection tables is not recommended: it would create another
operable Connection and blur consent/authority without preserving a single identity lineage.

**Decision needed:** approve the historical owner (edge archive or retained Actions tables),
who can read/recover it, and the retention/rollback window. Matching names do not authorize an
identity mapping; retain the two IDs as distinct unless separately evidenced and reviewed.
Selection of this work does not choose the archive model or authorize live maintenance.

## Implementation after the decision

1. Re-inventory both databases and readers, including all six tables, constraints, schema versions
   and source-writer absence. Record a bounded, secret-safe manifest identifying source/target,
   schema, counts and comparison outcome. Stop if source state or schema differs from the reviewed
   input. Seek October 23 maintenance evidence if available; the transfer-history uncertainty may
   remain explicit without discarding either dataset.
2. Implement the selected retention model with a new migration and a separately invoked bounded
   maintenance operation. For the proposed archive, copy exact rows into separately named historical
   relations, in one transaction, with source write fencing and a durable completion manifest.
   Verify every field and relationship. An identical rerun must verify equality; a differing prior
   copy fails without overwrite. The live edge tables must not be written by the archive operation.
   Do not weaken `mcp-edge-transfer`'s empty-destination guard or change landed migrations.
3. Test with the actual shape: one source Connection, three grant revisions including active and
   revoked rows, four expired enrollments, no source OAuth rows, plus distinct live edge identities
   and OAuth state. Cover mismatch/partial failure, safe rerun, exact field preservation, live-state
   preservation, and denial of archived grant/token use. Include schema upgrade/initialization and
   serving-role isolation checks.
4. Define the maintenance boundary, permissions and expected interruption before any live operation;
   obtain approval for that concrete operation. Back up both owners using the approved path and prove
   isolated restore under the [preservation policy](../docs/staging_data_preservation.md).
   Copying a stable legacy dataset into an isolated archive should not need to stop the live edge,
   but lock scope and access separation must be demonstrated before choosing that execution plan.
5. Execute only the reviewed operation, retain the comparison manifest, validate recovery from the
   chosen owner and verify live edge identities and ordinary authentication are unchanged. Recovery
   must not restore an old edge snapshot over subsequent OAuth writes. If validation fails, leave
   source and live edge intact; discard only an identified incomplete archive after review.

## Retirement after verified handoff and the retention window

Only then propose a separate new Actions migration dropping the six legacy tables. Recheck readers,
foreign keys and preserved ownership on the exact deployed revision. Remove transfer-only tooling
once it has no remaining caller; keep Actions' upstream OAuth/linkage, Action history, policies and
Web Push state. Verify both fresh and upgraded Actions databases and the relevant system tests.
Rollback is a tested source-schema/data restoration from the retained archive, not migration
`Down` alone, and never resumes the removed integrated Connection/OAuth writer. Source deletion
needs the reviewed retention decision and verified restore evidence; this plan executes neither.
