# Scan: Mypy-Appeasing Code Antipatterns

## Context

@../shared-context.md

## Pattern Description

Code written solely to satisfy mypy without adding semantic value. Often indicates misunderstanding of library types or unnecessary type manipulation.

## Examples of Antipatterns

### 1. Unnecessary casts

```python
# BAD: cast when return type is already correct
def list_states(self) -> dict[str, ServerEntry]:
    entries = await meta.list_states()  # Already returns dict[str, ServerEntry]
    return cast(dict[str, ServerEntry], entries)

# BAD: cast on model_dump which always returns dict
return cast(dict[str, Any], model.model_dump(mode="json"))
```

### 2. Assign-to-typed-variable (just for type annotation)

```python
# BAD: Variable exists only to annotate type
presets: dict[str, AgentPreset] = discover_presets(...)
return presets

# GOOD: Just return directly
return discover_presets(...)
```

### 3. Redundant isinstance assertions

```python
# BAD: Type is already known from prior check
if isinstance(item, AssistantMessageOut):
    msg: AssistantMessageOut = item
    text = msg.text
    assert isinstance(text, str)  # text is already str | None
    if text: return text

# GOOD: Use type directly
if isinstance(item, AssistantMessageOut):
    text = item.text
    if text: return text
```

### 4. Unnecessary TypeAdapter intermediate variables

```python
# BAD: TypeAdapter stored just to call once
adapter = TypeAdapter(dict[str, Any])
return adapter.validate_json(s)

# GOOD: Call directly
return TypeAdapter(dict[str, Any]).validate_json(s)
```

## Root Causes

Often these patterns appear because:

- **Not reading library source**: Assuming types are worse than they are
- **Cargo-culting**: Copying patterns without understanding
- **Outdated**: Code written for older library versions with worse typing
- **Missing type stubs**: Library has types but stubs aren't installed
- **Old library version**: Newer versions have better typing

## Before Assuming Types Are Bad: Research Library Improvements

When you find mypy-appeasing code, **always research if there's a better solution** before accepting the cast/workaround:

### 1. Check for Type Stubs Packages

```bash
# Search PyPI for type stubs
pip index versions types-{library}

# Common stub packages:
types-sqlalchemy      # SQLAlchemy ORM types
types-pygit2          # pygit2 Git library types
types-requests        # requests HTTP library
types-redis           # Redis client types
```

**Action**: If stubs exist, add to requirements and test if casts can be removed.

### 2. Check Library Version

```bash
# Find current version
pip show library-name

# Check latest version
pip index versions library-name

# Read changelog for typing improvements
# Often in CHANGELOG.md or release notes on GitHub
```

**Action**: If newer version has better types, update pinned version.

### 3. Check for Better Patterns/Helpers

Read library documentation for:

- Type-safe helper functions
- Generic/overload improvements in newer versions
- Recommended patterns in migration guides

**Example**: SQLAlchemy 2.0 added much better typing with `Mapped[]` annotations:

```python
# Old pattern (required casts)
users = cast(list[User], session.query(User).all())

# New pattern (no cast needed with SQLAlchemy 2.0)
users = session.scalars(select(User)).all()  # Properly typed!
```

### 4. Research Process for Each Cast

```python
# Found this cast:
result = cast(pygit2.Oid, repo.index.write_tree())

# Research steps:
# 1. Check current version
pip show pygit2  # Shows: 1.14.0

# 2. Check for type stubs
pip index versions types-pygit2  # Shows: 1.15.0.20250319 available!

# 3. Install and test
pip install types-pygit2
mypy file.py  # Does cast still needed?

# 4. If cast no longer needed, remove it
result = repo.index.write_tree()  # Now properly typed
```

### 5. Document Remaining Casts

If after research the cast is still needed:

```python
# Cast needed: pygit2.index.write_tree() returns Any in v1.14
# TODO: Recheck after updating to pygit2 1.16+
result = cast(pygit2.Oid, repo.index.write_tree())
```

## Detection Strategy

**Goal**: Find ALL instances of mypy-appeasing code (100% recall).

**Recall/Precision**:

- `grep "cast("` has ~100% recall for casts, ~95% precision (very few false positives)
- AST check for TypeAdapter has ~100% recall, ~80% precision (some legitimate one-time uses)
- Pattern matching for typed variables has ~70% recall, ~60% precision (many variations)

**Recommended approach**:

1. Run grep/AST to gather ALL candidates (casts, TypeAdapter, typed variables)
2. For each candidate: Research whether cast is actually necessary
   - Check library version (might be outdated)
   - Search for type stubs on PyPI
   - Read actual `.pyi` or source to see real types
   - Test removal with mypy
3. Fix confirmed unnecessary casts
4. Document remaining casts with reason (truly needed)

**Verification strategy**: Deep research required (low precision for "unnecessary")

- Even though grep has high recall, determining if cast is "unnecessary" requires library research
- Each candidate needs: version check, stub search, source reading, mypy test

**Recommended tools**:

### Grep Patterns

```bash
# Find casts (candidates only - must verify each)
rg --type py "cast\("

# Find typed variable assignments that immediately return
rg --type py -U "^\s+\w+:\s+\w+.*=.*\n\s+return \w+$"

# Find TypeAdapter intermediate variables
rg --type py -A1 "adapter.*=.*TypeAdapter"
```

### AST-Based Discovery (Optional)

Build tool that flags potential unnecessary casts:

- Visit Call nodes where func.id == 'cast'
- Extract cast target type and expression
- Flag for manual review with context

Strong coding LLM can build this from description. Use output as discovery only, not automatic fixes.

### Verification Process

For each cast found:

1. **Research library**: Version, stubs, documentation
2. **Read source**: Find actual return type in `.pyi` or source
3. **Test removal**: Remove cast, run mypy
4. **If mypy passes**: Cast was unnecessary, remove it
5. **If mypy fails**: Research if newer version/stubs would fix it

## Fix Strategy

### Before Removing Casts

1. **Read the actual source**: Check library .pyi or source for real return type
2. **Use mypy reveal_type**: Add `reveal_type(expr)` to see actual inferred type
3. **Test removal**: Remove cast and run mypy

### For Library Types

```python
# STEP 1: Find the library source
import openai
print(openai.__file__)  # Find installation location

# STEP 2: Read the actual type definition
# openai/types/responses.py or .pyi file
class Response(BaseModel):
    def model_dump(self, *, mode: Literal["json", "python"] = "python") -> dict[str, Any]:
        ...  # Returns dict[str, Any] - no cast needed!
```

## Example Fixes

### Cast Removal

```python
# Before:
return cast(dict[str, Any], model.model_dump(mode="json"))

# After (reading source shows model_dump returns dict[str, Any]):
return model.model_dump(mode="json")
```

### Variable Removal

```python
# Before:
entries: dict[str, ServerEntry] = await meta.list_states()
return entries

# After:
return await meta.list_states()
```

## Validation

```bash
# All fixes MUST pass mypy
mypy --strict path/to/file.py

# Check for remaining instances
rg --type py "cast\(|: \w+.*= .*\n.*return"
```

## When Casts Are Actually Needed

- **Third-party library with poor/missing types**: Consider contributing stubs
- **Complex protocol matching**: Where structural typing needs hint
- **Gradual typing migration**: Temporary during migration, document with TODO

Always add a comment explaining WHY the cast is needed:

```python
# cast needed: sqlalchemy relationship typing limitation
return cast(list[Model], query.all())
```
