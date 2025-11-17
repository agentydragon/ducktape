# Scan: type: ignore and noqa Suppressions

## Context
@../shared-context.md

## Pattern Description

Type checker and linter suppression comments (`# type: ignore`, `# noqa`) are often used to silence warnings without actually fixing the underlying issue. Most suppressions can be eliminated by properly typing the code.

## Why Suppressions Are Problematic

1. **Mask Real Bugs**: Type errors often indicate actual runtime bugs
2. **Type Safety Holes**: Create blind spots in type coverage
3. **Maintenance Burden**: Future refactorings miss type-checked code paths
4. **Code Smell**: Usually indicates architectural or typing issues

## Common Patterns and Fixes

### 1. Missing Type Imports/Annotations

**Problem**: Function claims to return one type but actually returns another

```python
# BAD: Masking type mismatch with ignore
async def responses_create_with_retries(client: AsyncOpenAI, **kwargs: Any) -> ResponsesResult:
    return await client.responses.create(**kwargs)  # type: ignore[return-value]
```

**Root Cause**: SDK returns `Response` but we claim to return `ResponsesResult`

**Fix**: Actually convert the type
```python
# GOOD: Proper conversion
async def responses_create_with_retries(client: AsyncOpenAI, **kwargs: Any) -> ResponsesResult:
    sdk_resp = await client.responses.create(**kwargs)
    return convert_sdk_response(sdk_resp)
```

### 2. Overly Broad Parameter Types

**Problem**: Function accepts `str | EnumType` but only needs `EnumType`

```python
# BAD: Accepting strings adds validation complexity
def to_reasoning_effort(value: ReasoningEffort | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, ReasoningEffort):
        return value.value
    try:
        effort = ReasoningEffort(value)  # Validate string
    except ValueError as exc:
        raise ValueError(f"Invalid: {value!r}") from exc
    return effort.value
```

**Then Later**:
```python
payload["effort"] = effort_value  # type: ignore[typeddict-item]
```

**Root Cause**: Return type is `str` but TypedDict expects `Literal["low", "medium", "high"]`

**Fix**: Narrow parameter types and return type
```python
# GOOD: Only accept enum, return literal type
def to_reasoning_effort(value: ReasoningEffort | None) -> ReasoningEffortLiteral | None:
    if value is None:
        return None
    return value.value  # StrEnum.value is the literal type

# Now this works without ignore:
payload["effort"] = effort_value
```

### 3. Missing Type for Row-Like Objects

**Problem**: Accessing database row without proper type

```python
# BAD: Accessing row indices without type
def _row_to_message(row: object) -> ChatMessage:
    return ChatMessage(
        id=str(row["id"]),  # type: ignore[index]
        ts=str(row["ts"]),  # type: ignore[index]
        author=ChatAuthor(str(row["author"])),  # type: ignore[index]
    )
```

**Root Cause**: Row type is `object` but actually is `aiosqlite.Row` which supports indexing

**Fix**: Import and use the actual type
```python
# GOOD: Proper Row type
from aiosqlite import Row

def _row_to_message(row: Row) -> ChatMessage:
    return ChatMessage(
        id=str(row["id"]),
        ts=str(row["ts"]),
        author=ChatAuthor(str(row["author"])),
    )
```

### 4. Accessing Private APIs

**Problem**: Using `_private_method()` from outside class

```python
# BAD: Accessing private implementation
async with self._p._open_row() as db:  # type: ignore[attr-defined]
    ...
```

**Root Cause**: Method `_open_row()` is private but needed by external code

**Fix Options**:
1. **Make method public** if it's intentionally part of the API
2. **Refactor to use public API** if one exists
3. **Create a public wrapper** if needed

This is an architectural decision - the `type: ignore` is a symptom of coupling to implementation details.

### 5. Meta-Ignores (unused-ignore)

**Problem**: `# type: ignore[unused-ignore]` means "ignore my ignore"

```python
# BAD: Meta-ignore is always suspicious
async def post(input: PostInput) -> PostResult:  # type: ignore[unused-ignore]
    ...
```

**Root Cause**: Someone added a type: ignore that mypy doesn't think is needed

**Fix**: Just remove it
```python
# GOOD: Remove unnecessary suppression
async def post(input: PostInput) -> PostResult:
    ...
```

**Test**: Run mypy - if it passes without the ignore, the ignore was unnecessary.

## Detection Strategy

### Find All Suppressions
```bash
# Count total
grep -r "type: ignore\|noqa" --include="*.py" . | wc -l

# Top files with most suppressions
grep -r "type: ignore\|noqa" --include="*.py" . | cut -d: -f1 | sort | uniq -c | sort -rn | head -20

# Group by suppression type
grep -ro "type: ignore\[[^]]*\]" --include="*.py" . | cut -d: -f2 | sort | uniq -c | sort -rn
```

### Verification Process

For each suppression found:

1. **Read the code context**: Understand what error is being suppressed
2. **Understand the type error**: What is mypy/ruff complaining about?
3. **Research the fix**: Is there a proper type annotation that would fix this?
4. **Test removal**: Comment out the ignore and run type checker
5. **Fix or document**: Either fix the underlying issue or add detailed comment explaining why suppression is needed

## Common Legitimate Suppressions

### Third-Party Library Limitations
```python
# type: ignore[attr-defined] - pygit2 1.14 missing type stubs for index.write_tree()
# TODO: Remove after upgrading to pygit2 1.16+ with better types
result = repo.index.write_tree()
```

### Intentional Dynamic Behavior
```python
# type: ignore[misc] - Intentionally dynamic: setattr used for metaprogramming
setattr(obj, dynamic_field_name, value)
```

### Gradual Migration
```python
# type: ignore[arg-type] - Legacy interface, refactoring to typed version in progress
# TODO(#1234): Remove after migrating all callers to new typed API
process_untyped_data(legacy_data)
```

## Fix Priority

1. **High Priority** (likely bugs):
   - `type: ignore[return-value]` - function returning wrong type
   - `type: ignore[arg-type]` - passing wrong argument type
   - `type: ignore[assignment]` - assigning incompatible type

2. **Medium Priority** (type safety holes):
   - `type: ignore[index]` - missing indexing support
   - `type: ignore[attr-defined]` - missing attribute/method
   - `type: ignore[typeddict-item]` - TypedDict field mismatch

3. **Low Priority** (style/meta):
   - `type: ignore[unused-ignore]` - meta-ignore (often just removable)
   - `noqa` without specific code - too broad, should be specific

## Validation

After removing suppressions:

```bash
# Run type checker
mypy --strict path/to/file.py

# Run linter
ruff check path/to/file.py

# Ensure tests pass
pytest path/to/tests/
```

## Example Session

```bash
# Find all type: ignore in a module
$ grep -n "type: ignore" adgn/src/adgn/openai_utils/retry.py
62:    return await client.responses.create(**kwargs)  # type: ignore[return-value]

# Investigate the error
$ mypy adgn/src/adgn/openai_utils/retry.py
error: Incompatible return value type (got "Response", expected "ResponsesResult")

# Research: Found convert_sdk_response() function in model.py
# Fix: Call the conversion function
$ git diff
-    return await client.responses.create(**kwargs)  # type: ignore[return-value]
+    sdk_resp = await client.responses.create(**kwargs)
+    return convert_sdk_response(sdk_resp)

# Validate fix
$ mypy adgn/src/adgn/openai_utils/retry.py
Success: no issues found
```

## Patterns to Watch For

### Pattern: Import Missing
- **Symptom**: `type: ignore[name-defined]`
- **Fix**: Add missing import

### Pattern: Forward Reference
- **Symptom**: `type: ignore[name-defined]` with class used before definition
- **Fix**: Use string literal `"ClassName"` or `from __future__ import annotations`

### Pattern: Circular Import
- **Symptom**: `type: ignore[attr-defined]` when importing from module that imports back
- **Fix**: Use `TYPE_CHECKING` block:
  ```python
  from typing import TYPE_CHECKING

  if TYPE_CHECKING:
      from .other_module import SomeType
  ```

### Pattern: Protocol Violation
- **Symptom**: `type: ignore[misc]` when implementing protocol incorrectly
- **Fix**: Properly implement all protocol methods with correct signatures

## Summary

**Golden Rule**: Every `type: ignore` or `noqa` should either be:
1. **Removed** by fixing the underlying issue, or
2. **Documented** with a detailed comment explaining why it's necessary

If you can't explain in one sentence why the suppression is needed, it probably shouldn't exist.
