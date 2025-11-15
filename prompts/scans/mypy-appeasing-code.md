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

## Detection Strategy

### AST Analysis for Casts

```python
import ast

class UnnecessaryCastDetector(ast.NodeVisitor):
    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id == 'cast'):
            # Check if cast target matches actual type
            # Requires type inference - use mypy's AST
            pass
```

### Grep Patterns

```bash
# Find casts
rg --type py "cast\("

# Find typed variable assignments that immediately return
rg --type py -U "^\s+\w+:\s+\w+.*=.*\n\s+return \w+$"

# Find TypeAdapter intermediate variables
rg --type py -A1 "adapter.*=.*TypeAdapter"
```

### Mypy Analysis

```bash
# Check if removing cast changes mypy output
# If mypy still passes without cast, it was unnecessary
```

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
