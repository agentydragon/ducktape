# Property: No Redundant Catch-and-Reraise

**Status:** Heuristic flag for agent consideration
**Kind:** outcome

## Pattern

Try/except that immediately re-raises the same exception without adding context, logging, or translation is often unnecessary and can be removed for clarity.

## Example

**Questionable:**

```python
try:
    docker_client.cleanup()
except docker.errors.APIError:
    # Cleanup failure: surface as API error
    raise
```

**Agent Guidance:**

- Flag such blocks as "consider removing the try/except and allow the exception to propagate."

## Exceptions (Allowed)

These cases are acceptable and should NOT be flagged:

- Adding structured logging/metrics or context
- Translating exception types (domain-specific)
- Narrow scoping to preserve invariants (e.g., guarantee finally semantics when language constructs don't suffice)

## Evidence to Include

When flagging, include:

- Exception type(s) being caught
- Whether body is empty/pass/`raise` only
- Whether any useful context/logging is present

## Detection Strategy

- AST check: `ast.Try` where each handler body is a bare `raise` (no message/logging) and except types are specific
- Leave nuanced "is this context useful?" judgment to the agent
- Purely structural detection: look for try/except with only `raise` in handler body
