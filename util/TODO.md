# Shared utility TODOs

## Schema reference traversal

Evaluate sharing schema-position traversal and local-reference lookup across the
[Claude hook comparison](../devinfra/claude/claude_api/json_schema.py),
[OpenAI strict-mode repair](../openai_utils/json_schema.py), and
[Loom submit schema](../x/loom/gym/baseline_llm.py). Keep the consumer projections
and their distinct policies:

- Claude expands direct local definitions for comparison, raises on missing local
  definitions, and lets reference siblings take precedence.
- OpenAI inlines only references with siblings. Bare recursive references and
  unresolved references remain intact. It rejects conflicting siblings outside its
  allowed metadata overrides.
- Loom builds a self-contained submit schema. It removes the root definitions,
  lets siblings take precedence, and flattens singleton `allOf`.

Agree which schema positions and local pointers each projection supports before
sharing code. Existing walkers can rewrite literal `$ref` objects under defaults,
examples, and unknown extension keywords, and can mistake a property named
`$ref` for a reference. A schema-position walker would change that behavior.
Cover literal payloads, nested definitions, unresolved references, bare and
annotated recursion, and conflicting siblings with independent consumer tests.
Keep any lossy projection explicit.

Haku's [routine exporter](../haku/console/mcp/export_tool_schemas.py) now writes
one Pydantic request schema for Ajv validation. Its old result catalog and
reference transformations were retired. [#18051](https://github.com/agentydragon/ducktape/pull/18051)
