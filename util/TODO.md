# Shared utility TODOs

## Schema reference traversal (schema audit B)

Evaluate a shared schema-position walker and local-reference resolver for
`util/json_schema.py`, `openai_utils/json_schema.py`, `loom/gym/baseline_llm.py`, and
`haku/console/mcp/export_tool_schemas.py`. Keep each consumer's named projection and explicit
missing-reference, cycle, and sibling-conflict policies. Haku's cycle approximation and result
unwrapping, OpenAI's sibling-only repair, and Loom's singleton `allOf` handling are distinct contracts.

Cover recursive and missing refs, conflicting siblings, nested definitions, and literal `$ref`
keys inside defaults/examples. The current comparison helper can recurse indefinitely on a missing
local definition; no current caller input triggering that was established. Do not replace runtime
adapters wholesale with that helper. Done: compatible traversal mechanics have one implementation,
with independent accepted/rejected payload tests and explicit treatment of lossy projections.
