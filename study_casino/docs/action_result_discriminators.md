# Action result discriminator rollout

`ActionResult` will use a `kind` literal on each variant and Pydantic's
`Field(discriminator="kind")`. The same models describe HTTP responses, generated
Zod readers, and results replayed from `ledger_events.result_json`.

## 1. Prepare readers

Each result variant accepts its own optional `kind`. A missing tag defaults to
`None`; `Field(exclude_if=...)` omits it when serializing, so existing writes and
responses keep their current shape. An explicit tag is validated and preserved.
The union remains structural, and the frontend schema generator carries the
optional literal fields to Zod without a handwritten parser.

Before phase 2, verify the image containing these readers is pinned and running
on every backend replica. Refresh or close existing browser tabs so active clients
use the corresponding frontend: a successful server rollout alone does not update
an already loaded JavaScript bundle. Do not emit tags while older readers remain.

## 2. Emit tags

Each variant's `kind` becomes a non-null literal with its literal value as the
default. Existing constructors then emit tags in HTTP responses and new ledger
results. The union still accepts older untagged ledger results by applying each
model's default; phase-1 readers accept the new tagged results during the roll.

Before phase 3, verify every backend replica runs a phase-2-compatible image.
Otherwise an older writer could insert an untagged result after backfilling.

## 3. Backfill and require discrimination

Add a new Alembic revision that fills missing tags on server-action ledger results
using their recorded action types. Preserve all result fields and ledger history;
do not rewrite existing migrations. Legacy client-sync results are a different
contract and remain untouched. Unknown server-action types must stop the migration
rather than silently guessing a tag.

Then use an annotated union with `Field(discriminator="kind")`. Fresh results
still use literal defaults in constructors; persisted JSON must contain the tag.
Generated Zod schemas consume the normal OpenAPI discriminator. No structural
fallback remains after this phase.

## Rollback

Phase 3 can roll back to phase 2, which reads tagged and untagged rows. Phase 2 can
roll back to phase 1, which accepts explicit tags. Returning to a pre-phase-1
reader requires first removing tagged writes and accounting for all tagged stored
results and active browser clients; do not roll directly back across that boundary.
