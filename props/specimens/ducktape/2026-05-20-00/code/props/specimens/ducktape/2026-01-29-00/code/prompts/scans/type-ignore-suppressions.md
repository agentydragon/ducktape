# Scan: Type Checker Suppression Comments

## Context

@../shared-context.md

## Pattern Description

Type checker suppressions (`# type: ignore`, `# noqa`) silence warnings without fixing underlying issues. Most can be eliminated through proper typing, revealing and preventing bugs.

**Key principle**: The goal is NOT just to remove type: ignore comments, but to improve the code. Use this hierarchy:

1. **BEST**: Clean code with no hacks for the type checker (proper types, refactoring, library upgrades)
2. **GOOD**: Type assertions/narrowing that make the type checker happy (`cast()`, `isinstance()`, `assert`, `getattr()`)
3. **ACCEPTABLE**: Well-documented `type: ignore` with a clear explanation of why it's necessary

Every suppression should either be eliminated through better code design, replaced with type assertions, or—if truly unavoidable—documented with a clear reason why it must remain.

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

### Pattern 3: Type Assertions for Dynamic Attributes

**BAD**: Suppressing attribute access on dynamic objects

```python
# Access undocumented internal session
session = c.api.session  # type: ignore[attr-defined]
```

**GOOD**: Use `hasattr()` check with `cast()` to declare type

```python
from typing import cast
import aiohttp

# Access undocumented internal session
if hasattr(c.api, "session"):
    session = cast(aiohttp.ClientSession, c.api.session)
```

**GOOD**: Use `cast()` when you know the type from function signature

```python
from typing import cast

# Declare that this is actually a NotifyingFastMCP client
# (when function already returns the right type but inference fails)
server = cast(NotifyingFastMCP, make_resources_server(...))
await server.broadcast_resource_list_changed()
```

### Pattern 4: Type Narrowing with Runtime Checks

**BAD**: Suppressing because type is too broad

```python
def process(client: Client) -> str:
    return client.special_method()  # type: ignore[attr-defined]
```

**GOOD**: Use `isinstance()` or `assert` to narrow type

```python
def process(client: Client) -> str:
    assert isinstance(client, SpecialClient)
    return client.special_method()  # Type checker knows it's SpecialClient now
```

**GOOD**: Declare specific type when you control the call site

```python
def process(client: SpecialClient) -> str:
    return client.special_method()  # No assertion needed
```

### Pattern 5: Missing Type Import

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

### Pattern 6: Meta-Ignores (unused-ignore)

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

**MANDATORY Step 0**: Discover ALL type checker and linter suppression comments in the codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL suppression output using your intelligence
- High recall required, high precision NOT required - you determine which are fixable
- Review each suppression for: missing imports, type conversions, proper types, architecture issues
- Prevents lazy analysis by forcing examination of ALL type checker workarounds

```bash
# Find ALL type checker suppression comments with context
rg --type py '# type: ignore' -B 2 -A 1 --line-number
rg --type py '# noqa' -B 2 -A 1 --line-number
rg --type py '# pyright: ignore' -B 2 -A 1 --line-number
rg --type py '# pylint: disable' -B 2 -A 1 --line-number
rg --type py '# mypy:' -B 2 -A 1 --line-number

# Count total suppressions found
(rg --type py '# type: ignore' && rg --type py '# noqa' && rg --type py '# pyright: ignore' && rg --type py '# pylint: disable' && rg --type py '# mypy:') | wc -l
```

**What to review for each suppression:**

1. **Missing Type Conversion**: Return type mismatch that needs conversion function
2. **Overly Broad Types**: Parameter/return types that should be narrowed
3. **Type Assertions Needed**: Dynamic attributes or runtime checks needed
4. **Type Narrowing**: Use `isinstance()`, `assert`, or `hasattr()` checks
5. **Legitimate Suppressions**: AST visitors, monkey-patching, library limitations (keep with docs)

**Process ALL output**: Read each suppression, use your judgment to identify which can be fixed using the hierarchy below.

**Fix Hierarchy (BEST → ACCEPTABLE)**:

1. **BEST**: Clean code with no hacks (proper types, refactoring, library upgrades)
2. **GOOD**: Type assertions/narrowing (`cast()`, `isinstance()`, `assert`, `hasattr()`)
3. **ACCEPTABLE**: Well-documented suppression with clear explanation why necessary

---

**Recall/Precision**:

- Finding suppressions: ~100% recall, ~100% precision
- Determining if "fixable": Requires code analysis (lower precision)
- Some patterns have clear fixes (missing imports, type conversions)
- Others require architectural changes (private API access)

**Tool characteristics**:

- Finding comments: 100% recall, 100% precision
- Determining "necessary": Requires verification
- Some patterns have clear fixes (missing imports, type conversions)
- Others require architectural changes (private API access)

## Verification Process

For each suppression found, apply this hierarchy:

1. **Read context**: Understand what error is being suppressed.
2. **Research the BEST fix** (clean code, no hacks):
   - Check if type conversion function exists
   - Check if proper type can be imported
   - Check if parameter types can be narrowed
   - Check library version (may have better types now)
   - Check if function signature can be improved
3. **Try GOOD fix** (type assertions/narrowing):
   - Use `cast()` to declare actual type
   - Use `isinstance()` or `assert` for runtime narrowing
   - Use `hasattr()` check before accessing dynamic attributes
   - Add specific type annotations to make types more precise
4. **Test removal**: Comment out suppression and run type checker
5. **Document if necessary** (ACCEPTABLE last resort):
   - If legitimately needed: Add detailed comment explaining why
   - Examples: Monkey-patching, library limitations, AST visitor pattern

**Priority**: Always prefer improving the code over adding type assertions, and prefer type assertions over keeping suppressions.

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
