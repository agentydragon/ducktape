# Scan: Type Checker Suppression Comments

## Context
@../shared-context.md

## Pattern Description

Type checker suppressions (`# type: ignore`, `# noqa`) silence warnings without fixing underlying issues. Most can be eliminated through proper typing, revealing and preventing bugs.

**Key principle**: Every suppression should either be removed by fixing the code, or documented with a clear reason why it's necessary.

## Why Suppressions Are Problematic

- **Mask Real Bugs**: Type errors often indicate runtime bugs
- **Create Blind Spots**: Type checker can't help in suppressed areas
- **Maintenance Burden**: Future refactorings miss type-checked paths
- **Code Smell**: Usually indicate fixable typing or architectural issues

## Common Fixable Patterns

### Pattern 1: Missing Type Conversion

**BAD**: Suppressing return type mismatch
```python
async def responses_create_with_retries(client: AsyncOpenAI, **kwargs: Any) -> ResponsesResult:
    return await client.responses.create(**kwargs)  # type: ignore[return-value]
```

**Issue**: SDK returns `Response` but function claims to return `ResponsesResult`

**GOOD**: Actually convert the type
```python
async def responses_create_with_retries(client: AsyncOpenAI, **kwargs: Any) -> ResponsesResult:
    sdk_resp = await client.responses.create(**kwargs)
    return convert_sdk_response(sdk_resp)
```

### Pattern 2: Overly Broad Type Annotations

**BAD**: Accepting broader types than needed
```python
def to_effort(value: ReasoningEffort | str | None) -> str | None:
    # Complex validation to handle strings...
    ...

# Later:
payload["effort"] = effort_value  # type: ignore[typeddict-item]
```

**Issue**: Return type is `str` but TypedDict expects `Literal["low", "medium", "high"]`

**GOOD**: Narrow parameter and return types
```python
def to_effort(value: ReasoningEffort | None) -> ReasoningEffortLiteral | None:
    if value is None:
        return None
    return value.value  # StrEnum.value is the literal type

# Now works without ignore:
payload["effort"] = effort_value
```

### Pattern 3: Missing Type Import

**BAD**: Using `object` when actual type is known
```python
def _row_to_message(row: object) -> ChatMessage:
    return ChatMessage(
        id=str(row["id"]),  # type: ignore[index]
        ts=str(row["ts"]),  # type: ignore[index]
    )
```

**Issue**: Row is actually `aiosqlite.Row` which supports indexing

**GOOD**: Import and use actual type
```python
from aiosqlite import Row

def _row_to_message(row: Row) -> ChatMessage:
    return ChatMessage(
        id=str(row["id"]),
        ts=str(row["ts"]),
    )
```

### Pattern 4: Meta-Ignores (unused-ignore)

**BAD**: Suppressing the suppression
```python
async def post(input: PostInput) -> PostResult:  # type: ignore[unused-ignore]
    ...
```

**Issue**: Someone added `type: ignore` that mypy doesn't think is needed

**GOOD**: Remove unnecessary suppression
```python
async def post(input: PostInput) -> PostResult:
    ...
```

**Test**: Run mypy - if it passes without the ignore, remove it.

## Detection Strategy

**Goal**: Find ALL suppression comments (100% recall).

**Recall/Precision**:
- `grep "type: ignore\|noqa"` has ~100% recall, ~100% precision for finding suppressions
- Determining if suppression is "necessary" requires code analysis (lower precision)

**Recommended approach**:
1. Run grep to find all suppression comments (100% recall)
   ```bash
   grep -rn "type: ignore\|noqa" --include="*.py" .
   ```
2. Group by file and suppression type to identify patterns
3. For each suppression:
   - Read surrounding code to understand the error
   - Determine if fixable (missing import, wrong type, etc.)
   - Try removing suppression and running type checker
   - Either fix underlying issue or document why needed
4. Verification strategy:
   - **Potentially fixable**: Deep investigation (check library types, imports, conversions)
   - **Intentional (AST visitor, side-effect imports)**: Verify legitimacy, keep with clear comment
   - **Private API access**: Architectural decision, may need refactoring

**Tool characteristics**:
- Finding comments: 100% recall, 100% precision
- Determining "necessary": Requires verification
- Some patterns have clear fixes (missing imports, type conversions)
- Others require architectural changes (private API access)

## Verification Process

For each suppression found:

1. **Read context**: Understand what error is being suppressed
2. **Research the fix**:
   - Check if type conversion function exists
   - Check if proper type can be imported
   - Check if parameter types can be narrowed
   - Check library version (may have better types now)
3. **Test removal**: Comment out suppression and run type checker
4. **Fix or document**:
   - If fixable: Fix and remove suppression
   - If needed: Add detailed comment explaining why

## Common Legitimate Suppressions

Some suppressions are necessary and should be kept (with documentation):

### AST Visitor Pattern
```python
class Visitor(ast.NodeVisitor):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        # Method name must match AST node type per visitor pattern
        ...
```

### Side-Effect Imports
```python
from . import (
    detector_a,  # noqa: F401  - imported for registration side effect
    detector_b,  # noqa: F401
)
```

### Private API (with TODO)
```python
async with self._p._open_row() as db:  # type: ignore[attr-defined]
    # TODO: Make _open_row() public or use public API
    ...
```

### Library Limitations (with version note)
```python
result = repo.write_tree()  # type: ignore[attr-defined]
# pygit2 1.14 missing type stubs for write_tree()
# TODO: Remove after upgrading to pygit2 1.16+
```

## Priority for Fixing

**High Priority** (likely bugs):
- `type: ignore[return-value]` - returning wrong type
- `type: ignore[arg-type]` - passing wrong argument
- `type: ignore[assignment]` - incompatible assignment

**Medium Priority** (type safety):
- `type: ignore[index]` - missing indexing support
- `type: ignore[attr-defined]` - missing attribute
- `type: ignore[typeddict-item]` - TypedDict field mismatch

**Low Priority** (cleanup):
- `type: ignore[unused-ignore]` - meta-ignore (often removable)
- `noqa` without code - make specific

## Grep Patterns

Find all suppressions:
```bash
# Count total
grep -r "type: ignore\|noqa" --include="*.py" . | wc -l

# Group by file (find files with most suppressions)
grep -r "type: ignore\|noqa" --include="*.py" . | cut -d: -f1 | sort | uniq -c | sort -rn

# Group by suppression type
grep -ro "type: ignore\[[^]]*\]" --include="*.py" . | cut -d: -f2 | sort | uniq -c | sort -rn
```

Find specific types:
```bash
# All return-value ignores
rg --type py "type: ignore\[return-value\]"

# All attribute access ignores
rg --type py "type: ignore\[attr-defined\]"

# All meta-ignores
rg --type py "type: ignore\[unused-ignore\]"
```

## Validation

After removing suppressions, verify:

```bash
# Type check passes
mypy --strict path/to/file.py

# Linter passes
ruff check path/to/file.py

# Tests still pass
pytest path/to/tests/
```

## Example Fix Session

```bash
# Find suppressions in module
$ rg -n "type: ignore" openai_utils/retry.py
62:    return await client.responses.create(**kwargs)  # type: ignore[return-value]

# Check what error it's suppressing
$ mypy openai_utils/retry.py
error: Incompatible return value type (got "Response", expected "ResponsesResult")

# Research: Found convert_sdk_response() in model.py
# Fix: Call conversion function
$ git diff
-    return await client.responses.create(**kwargs)  # type: ignore[return-value]
+    sdk_resp = await client.responses.create(**kwargs)
+    return convert_sdk_response(sdk_resp)

# Validate
$ mypy openai_utils/retry.py
Success: no issues found
```

## Summary

**Golden Rule**: Every suppression should either be:
1. **Removed** by fixing the underlying issue, or
2. **Documented** with a comment explaining why it's necessary

If you can't explain in one sentence why the suppression is needed, investigate deeper - it's likely fixable.
