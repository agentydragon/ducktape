---
name: mcp-design
description: "Design an MCP server tool surface for an AI agent. Starts from common workflows, derives tool schemas, audits for completeness and safety. Use when designing a new MCP server or redesigning an existing one."
argument-hint: "[path to MCP server code or service being wrapped]"
---

# MCP Server Design

Design or redesign the tool surface of an MCP server for AI agent use.
The goal is a tool surface that is hard to misuse, natural for common
workflows, and covers the full API of the service being wrapped.

**Argument:** `$ARGUMENTS` (path to MCP server code or description of
the service being wrapped)

## Reference Resources

Before starting, familiarize yourself with these:

- **Anthropic tool design guide**: https://www.anthropic.com/engineering/writing-tools-for-agents
- **Anthropic tool definition docs**: https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools
- **MCP spec — tools**: https://modelcontextprotocol.io/docs/concepts/tools
- **Six-tool pattern**: https://www.mcpbundles.com/blog/mcp-tool-design-pattern

Key takeaways from these resources:

- Descriptions are the single most important factor in tool performance.
  3-4 sentences minimum per tool.
- Fewer tools with good parameters beats many similar tools.
- Return only high-signal data. Bloated responses waste context.
- Error messages must be actionable, not opaque.
- Don't mirror REST 1:1. Design around workflows.

## Design Principles

Apply these throughout the design process:

### 1. Hard to use incorrectly

If the agent omits something important, that should be an error — not a
silent default. Validate inputs early. Use enums/Literal types for
constrained values. Require explicit values for anything safety-critical
(e.g., location when managing multi-location inventory, unit when dealing
with quantities).

### 2. No surprising footguns

Common API footguns to watch for:

- **Full-replace "edit"**: sending only changed fields nulls out the rest.
  Use partial updates (server-side read-merge-write) for edit operations.
- **Silent defaults**: omitting a field silently picks a default that may
  be wrong. Make important fields required with clear errors.
- **Null ambiguity**: `null` meaning both "unchanged" and "clear this field"
  in partial updates. Use a separate `clear_fields: set[FieldEnum]`
  parameter for nullable fields.
- **Opaque IDs**: requiring numeric IDs when the agent (and user) think in
  names. Accept both: `int | str` where int=ID, str=name.

### 3. Common workflows should be natural

The number of tool calls for a routine operation should be small. But
consolidation must not come at the cost of safety — a single call that
does too much implicitly is worse than two explicit calls.

### 4. Descriptions are the #1 lever

Each tool description must answer:

- When should I use this tool? (and when should I NOT?)
- What do I need to provide?
- What will I get back?
- What can go wrong?

Do NOT describe implementation details ("sends POST to /api/foo
concurrently"). DO describe the agent-facing behavior.

### 5. Return only what's needed for the next step

- Include names alongside IDs so the agent can present results to the
  user without extra lookups.
- Compact responses by default. Offer `detail: "brief" | "full"` when
  both overview and deep-dive use cases exist.
- Pre-compute derived values the agent would need (e.g., `days_until_expiry`
  instead of making the agent do date math).

### 6. Names are first-class

Accept `int | str` for entity references: int = ID, str = resolved by
name. Error on not-found with suggestions. Check whether the underlying
service enforces unique names — if so, resolution is unambiguous.

### 7. Units and dimensions are always explicit

Every request and response involving quantities must name the unit. Never
assume the agent (or user) knows what unit a number is in.

### 8. Error messages teach correct usage

Every error should name what's wrong, suggest the fix, and list available
options. The agent should be able to recover in one retry without a
separate lookup call.

## Design Process

### Phase 1: Understand the service

1. **Read the API surface** of the service being wrapped. OpenAPI specs,
   database schemas, source code — whatever is available.
2. **Identify the data model**: entities, relationships, constraints
   (unique names? required fields? nullable columns?).
3. **Note the API's own quirks**: unreliable schemas, fields that crash
   when null, response types that don't match the spec, missing batch
   endpoints.

### Phase 2: Design from workflows

Do NOT start from the API endpoints. Start from **user workflows** —
what will an agent actually be asked to do?

For each workflow:

1. Write out the ideal conversation: user request → agent tool calls →
   responses → agent reply to user.
2. Count the round-trips. Is the common case 1-3 calls? If it's 6+,
   the tool surface is too granular.
3. Check error paths: what if the product doesn't exist? Wrong unit?
   Ambiguous name? Can the agent recover in one retry?

Structure the design doc around these workflow scenarios. Example scenarios
to consider (adapt to your domain):

- **Happy path**: the most common operation, everything exists
- **Bootstrap**: first-time setup, nothing exists yet
- **Error recovery**: wrong name, missing field, constraint violation
- **Batch operations**: do the same thing for 10 items
- **Query/overview**: "show me everything" / "what's the status?"
- **Partial update**: change one field without touching others
- **Undo/rollback**: reverse a recent operation

### Phase 3: Derive tool schemas

From the workflow scenarios, derive the tool inventory:

1. **Group related operations**. Don't create `create_foo`, `create_bar`,
   `create_baz` when a single `create_entity(type, ...)` works. But DO
   create dedicated tools for complex entities that benefit from typed
   schemas and validation.

2. **Define input schemas** using Pydantic models. Use:
   - `int | str` for entity references (ID or name)
   - `Literal` / `StrEnum` for constrained values
   - Required fields for safety-critical params
   - `Field(description=...)` for every non-obvious parameter
   - `model_validator` for cross-field constraints

3. **Define output schemas**. Use discriminated unions for results:
   `{kind: "ok", ...} | {kind: "error", error: str}`. Always include
   names alongside IDs. Keep responses compact.

4. **Decide batch vs single**. Batch tools (accept `list[Item]`) are
   good for operations the agent often does N times. Each item succeeds
   or fails independently. Single-item tools are fine for rare operations.

### Phase 4: Audit for completeness

Systematically check that the tool surface is complete:

1. **Schema field coverage**: for each tool, check that input/output
   fields cover the important fields in the underlying service. Don't
   expose every field — but don't miss the ones that matter for common
   workflows.

2. **Impossible operations**: walk through the full API surface of the
   underlying service. Is there any common operation that's impossible
   with the proposed tools, even via escape hatches (generic CRUD)?

3. **Data model coverage**: for each entity type in the service, check:
   can you create it? Read it? Update it? Delete it? If not via a
   dedicated tool, via the generic escape hatch?

4. **Partial update safety**: for every "edit" tool, verify that omitting
   a field means "keep unchanged", not "set to null". If some fields can
   legitimately be cleared, use `clear_fields: set[FieldEnum]`.

5. **Conversion/validation**: if the service has unit conversions,
   permission checks, or other validation that the agent might trigger,
   check that the MCP server handles it gracefully (server-side
   conversion, clear errors) rather than passing through opaque failures.

### Phase 5: Audit for safety

Check each tool against these antipatterns:

- [ ] **Silent defaults**: does omitting a field silently pick a value
      the agent didn't choose?
- [ ] **Full-replace edits**: does an "edit" operation null out fields
      the agent didn't mention?
- [ ] **Implicit state changes**: does a read operation trigger side
      effects? Does a create operation modify other entities?
- [ ] **Retry safety**: are mutating operations (POST) separated from
      follow-up reads (GET) in the retry logic? Retrying a bundled
      POST+GET can cause double-mutations.
- [ ] **Unbounded responses**: can a "list all" call return thousands
      of items? Add pagination or filtering.
- [ ] **Opaque errors**: does the agent get back "400 Bad Request" or
      an actionable message with suggestions?

### Phase 6: Write descriptions

For each tool, write the description the agent will see. This is not
the implementation docstring — it's the agent's instruction manual.

Template:

```
[What this tool does in one sentence.]

[When to use it — and when NOT to (use X instead).]

[What the response looks like — key fields and their meaning.]

[Common pitfalls: required fields, constraints, error cases.]
```

Test: could an agent that has never seen this service before use the
tool correctly on its first try, given only the description and schema?

## OpenAPI-Generated vs Custom Tools

When the underlying service has a well-designed OpenAPI spec:

- **Use OpenAPI-generated tools** for simple, well-specified endpoints
  (CRUD on simple entities, read-only queries).
- **Use custom tools** when:
  - The OpenAPI spec is unreliable (wrong types, missing fields, crashes)
  - The operation involves amounts/units that need validation
  - Batch operations are needed (OpenAPI is always single-item)
  - Partial update semantics are needed (OpenAPI PUT is full-replace)
  - The response needs enrichment (resolve IDs to names, pre-compute
    derived values)
  - Multiple API calls need to be composed into one agent-facing tool

Strip `output_schema` from OpenAPI-generated tools if the service's
response schemas are unreliable. E2e tests are the real contract.

## Deliverables

The design process should produce:

1. **Design doc** (`docs/agent_interaction_design.md` or similar):
   - Design principles
   - Workflow scenarios with ideal tool call sequences
   - Proposed tool inventory with schemas
   - Key design decisions with rationale
   - Delta from current implementation (if redesigning)
   - Future work / not-yet-covered features

2. **Tool inventory** with for each tool:
   - Name
   - Input schema (Pydantic model or equivalent)
   - Output schema
   - Description (agent-facing)

3. **Audit checklist** confirming:
   - All common workflows are covered in 1-3 calls
   - No impossible operations for the service's core features
   - No silent-default or full-replace footguns
   - Error messages are actionable
   - Descriptions are sufficient for first-try correct usage
