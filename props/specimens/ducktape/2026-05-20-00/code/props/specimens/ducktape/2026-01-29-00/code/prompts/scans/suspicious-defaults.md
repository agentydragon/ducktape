# Scan: Suspicious Default Values

## Context

@../shared-context.md

## Pattern Description

Using `or` operators to provide default values (like `x or {}`, `x or ""`) often masks underlying type safety issues. These patterns suggest that the API internally allows `None` but then quietly coerces it to a different "null value" at runtime, creating a mismatch between the type signature and actual behavior.

**Key principle**: If a value can be `None`, the type signature should reflect that. If it cannot be `None`, don't use defensive `or` defaults.

## Examples of Antipatterns

### BAD: Masking Optional Types with `or ""`

```python
# BAD: Type says description can be None, but we convert None to ""
from mcp import types as mcp_types

tools = await client.list_tools()
for t in tools:
    # t.description: str | None per upstream type
    tools_payload.append(
        FunctionToolParam(
            name=t.name,
            description=t.description or "",  # Masking None
            parameters=t.inputSchema
        )
    )
```

**Why this is bad**:

- `FunctionToolParam` constructor accepts `description: str | None`
- Using `or ""` suggests we're defensive about None, but type should handle it
- The `or ""` is hiding type information from the type checker
- If `description=None` is valid, pass it explicitly; if not, handle it properly

**Better - Option 1: Explicit None handling**:

```python
# GOOD: FunctionToolParam accepts None, so pass it through
tools = await client.list_tools()
for t in tools:
    tools_payload.append(
        FunctionToolParam(
            name=t.name,
            description=t.description,  # None is fine if type allows it
            parameters=t.inputSchema
        )
    )
```

**Better - Option 2: Explicit default if semantically required**:

```python
# GOOD: If empty string has semantic meaning different from None
tools = await client.list_tools()
for t in tools:
    tools_payload.append(
        FunctionToolParam(
            name=t.name,
            description=t.description if t.description is not None else "",
            parameters=t.inputSchema
        )
    )
# But document WHY empty string is semantically different from None
```

### BAD: Defensive `or {}` when type says non-None

```python
# BAD: Type says parameters is dict[str, Any], but we defend against None
from mcp import types as mcp_types

tools = await client.list_tools()
for t in tools:
    # t.inputSchema: dict[str, Any] per upstream type (NOT dict[str, Any] | None)
    tools_payload.append(
        FunctionToolParam(
            name=t.name,
            description=t.description,
            parameters=t.inputSchema or {}  # Type says can't be None!
        )
    )
```

**Why this is bad**:

- `t.inputSchema` is typed as `dict[str, Any]` (not `dict[str, Any] | None`)
- Using `or {}` suggests either:
  1. The upstream type annotation is lying (runtime can return None)
  2. We're being unnecessarily defensive
  3. There's a version mismatch or API misunderstanding
- This masks a potential type safety issue

**Better - Option 1: Trust the type**:

```python
# GOOD: If type says non-None, trust it
tools = await client.list_tools()
for t in tools:
    tools_payload.append(
        FunctionToolParam(
            name=t.name,
            description=t.description,
            parameters=t.inputSchema  # Trust the type
        )
    )
```

**Better - Option 2: Fix upstream or add type assertion**:

```python
# GOOD: If upstream type is wrong, fix it or assert
from typing import cast

tools = await client.list_tools()
for t in tools:
    # If inputSchema can actually be None at runtime despite types,
    # either fix upstream or make it explicit:
    schema = t.inputSchema if t.inputSchema is not None else {}
    tools_payload.append(
        FunctionToolParam(
            name=t.name,
            description=t.description,
            parameters=schema
        )
    )
    # And file a bug report about the type mismatch!
```

### BAD: Runtime enforcement with `or` instead of type annotation

```python
# BAD: Type enforcement hidden in implementation
def process_config(headers: dict[str, str] | None) -> None:
    # Runtime coercion masking type issue
    actual_headers = headers or {}
    for key, value in actual_headers.items():
        # ... process headers
```

**Why this is bad**:

- The function accepts `None` but immediately converts it to `{}`
- This suggests `None` and `{}` mean the same thing
- Better to use `= {}` default or make it non-None

**Better**:

```python
# GOOD: Make intent clear with default parameter
def process_config(headers: dict[str, str] | None = None) -> None:
    if headers is None:
        headers = {}
    for key, value in headers.items():
        # ... process headers

# OR even better: Non-None with default
def process_config(headers: dict[str, str] = {}) -> None:  # Note: mutable default!
    # Actually use Field(default_factory=dict) in Pydantic or:
def process_config(headers: dict[str, str] | None = None) -> None:
    headers = headers or {}
    # But document that None == empty dict semantically
```

### BAD: Masking type enforcement in data flow

```python
# BAD: Type says inserts_input is specific union, but code doesn't enforce
class Continue:
    inserts_input: tuple[InputItem | FunctionCallItem | ..., ...] = ()

# In agent.py:
for it in decision.inserts_input:
    # Runtime isinstance check instead of type enforcement
    if isinstance(it, UserMessage | AssistantMessage | SystemMessage | FunctionCallItem):
        self._transcript.append(it)
```

**Why this is bad**:

- The type says `inserts_input` contains specific types
- Runtime code does an isinstance check suggesting we don't trust the type
- This indicates either:
  1. Type annotation is too loose (allows wrong types)
  2. Runtime check is unnecessary (type already enforces it)
  3. Type annotation is lying

**Better**:

```python
# GOOD: Tighten type annotation to match runtime checks
class Continue:
    inserts_input: tuple[
        UserMessage | AssistantMessage | SystemMessage | FunctionCallItem,
        ...
    ] = ()

# In agent.py:
for it in decision.inserts_input:
    # No isinstance check needed - type system enforces it
    self._transcript.append(it)
```

### BAD: String formatting hiding None

```python
# BAD: Convert None to empty string silently
def format_error(message: str | None) -> str:
    return f"Error: {message or ''}"
```

**Better**:

```python
# GOOD: Handle None explicitly
def format_error(message: str | None) -> str:
    if message is None:
        return "Error: (no message)"
    return f"Error: {message}"

# OR: Don't accept None if it doesn't make sense
def format_error(message: str) -> str:
    return f"Error: {message}"
```

## Detection Strategy

**MANDATORY Step 0**: Discover ALL function parameter defaults and field defaults in the codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL default value output using your intelligence
- High recall required, high precision NOT required - you determine which defaults are suspicious
- Review each for: appropriate optionality, semantic meaning, type consistency
- Prevents lazy analysis by forcing examination of ALL default value decisions

```bash
# Find ALL function parameter defaults with context
rg --type py 'def \w+\([^)]*=' -B 1 -A 2 --line-number

# Find ALL field defaults (class attributes with = assignments)
rg --type py '^[[:space:]]+\w+:.*=' -B 2 -A 1 --line-number

# Find dataclass field defaults
rg --type py '@dataclass' -A 10 --line-number

# Count total defaults found
echo "Function defaults:" && rg --type py 'def \w+\([^)]*=' | wc -l
echo "Field defaults:" && rg --type py '^[[:space:]]+\w+:.*=' | wc -l
```

**What to review for each default:**

1. **Optionality**: Does it make sense for this to be optional in this context?
2. **Semantic meaning**: Is default value semantically appropriate (e.g., `=0` for count vs `=None`)?
3. **Type consistency**: Does default match the declared type?
4. **Mutable defaults**: Is this `=[]` or `={}` without `field(default_factory=...)`? (BUG!)
5. **Defensive `or` patterns**: Is there both a default AND `x or default` usage? (redundant)

**Process ALL output**: Read each default, use your judgment to identify suspicious patterns.

**Common suspicious patterns to watch for:**

- `param: str = ""` when None would be more honest
- `param: dict = {}` (mutable default bug in non-dataclass)
- `param: T | None = None` but immediately converted with `or default`
- `count: int = 0` when semantically it should be required
- `field: list = []` (mutable default bug)

---

**Goal**: Find ALL instances of suspicious defaults (high recall target ~80-90%).

**Recall/Precision**: High recall (~90%) with automation, medium precision (~60%)

- `grep -E "or \{\}|or \"\"|or ''"` finds most instances: ~90% recall, ~60% precision
- `grep -E "or \[\]"` finds list defaults: ~90% recall, ~60% precision
- False positives: Legitimate use of `or` for boolean coercion, ternary-like patterns

**Why high recall**:

- Pattern is syntactically distinct and easy to grep
- Almost all instances follow `x or <literal>` pattern
- Variations are rare (mostly just `{}`, `""`, `[]`, `()`)

**Recommended approach AFTER Step 0**:

1. Run grep to find all `or {}`, `or ""`, `or []` patterns (~90% recall, ~60% precision)
2. For each candidate, analyze:
   - **What is the type of the value being checked?**
     - If typed as `T | None`, is the default appropriate?
     - If typed as non-None, why the defensive check?
   - **What does the target accept?**
     - Check the parameter/field type at the call site
     - Does it accept None? Then maybe pass None instead
   - **Is this legitimate boolean coercion?**
     - Some uses like `count or 0` might be intentional
   - **Is there semantic difference?**
     - Does `None` vs `{}` have different meaning?
3. Fix confirmed suspicious defaults
4. Accept ~10% false positives from boolean coercion patterns

**High-recall retriever**:

```bash
# Find all "or literal" patterns
rg --type py "or \{\}|or \"\"|or ''|or \[\]|or \(\)" --line-number

# With context to see the types
rg --type py -B2 -A1 "or \{\}|or \"\"|or ''|or \[\]|or \(\)"

# Find in specific constructs (function calls, assignments)
rg --type py "= .* or \{\}"
rg --type py "\([^)]*or \{\}[^)]*\)"
```

**Verification for each candidate**:

1. **Check the source type**:

   ```python
   value = get_something()  # What type does get_something() return?
   result = value or {}     # Is it T | None? Or just T?
   ```

2. **Check the target type**:

   ```python
   def func(param: dict[str, Any] | None): ...
   func(value or {})  # Does func accept None? If yes, why convert?
   ```

3. **Look for upstream type issues**:

   ```python
   # Is the source type annotation wrong?
   # Example: inputSchema: dict[str, Any] but can actually be None
   x = obj.inputSchema or {}  # Bug: type says non-None but we defend against None!
   ```

4. **Consider semantic difference**:

   ```python
   # Is there a semantic difference between None and empty?
   headers = config.headers or {}
   # If None means "use default headers" and {} means "no headers",
   # then this might be a bug - they're not equivalent!
   ```

## Fix Strategy

### Fix 1: Remove defensive default if type is non-None

```python
# Before:
# t.inputSchema: dict[str, Any] (type says non-None)
parameters = t.inputSchema or {}

# After:
parameters = t.inputSchema  # Trust the type
```

### Fix 2: Handle None explicitly if type is nullable

```python
# Before:
# t.description: str | None
description = t.description or ""

# After (if None should become empty string):
description = t.description if t.description is not None else ""

# OR (if None is acceptable downstream):
description = t.description  # Pass None through
```

### Fix 3: Tighten type annotation to match runtime checks

```python
# Before:
class Continue:
    inserts_input: tuple[InputItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut, ...] = ()

# Runtime:
for it in decision.inserts_input:
    if isinstance(it, UserMessage | AssistantMessage | SystemMessage | FunctionCallItem):
        self._transcript.append(it)

# After:
# Tighten type to only what's actually valid:
class Continue:
    inserts_input: tuple[
        UserMessage | AssistantMessage | SystemMessage | FunctionCallItem,
        ...
    ] = ()

# Runtime (no check needed):
for it in decision.inserts_input:
    self._transcript.append(it)  # Type system enforces correctness
```

### Fix 4: Use default parameters instead of `or`

```python
# Before:
def process(data: dict[str, Any] | None) -> None:
    data = data or {}
    # ...

# After:
def process(data: dict[str, Any] | None = None) -> None:
    if data is None:
        data = {}
    # ...

# OR with default parameter:
def process(data: dict[str, Any] | None = None) -> None:
    actual_data = {} if data is None else data
    # ...
```

## When `or` Defaults Are Acceptable

Cases where `x or default` is correct:

### 1. Boolean coercion for counts/numbers

```python
# OK: Treat 0 as "use default"
count = config.get_count() or 10  # 0 means "use default 10"
```

### 2. Explicit falsy-to-default for strings

```python
# OK: Empty string means "use default"
name = user_input.strip() or "Unnamed"  # Empty string → default
```

### 3. Backward compatibility shim

```python
# OK: Legacy code during migration
# TODO(2025-12): Remove after all callers updated
def legacy_api(data: dict[str, Any] | None = None) -> None:
    data = data or {}  # Compatibility: treat None as empty
```

### 4. Intentional equivalence of None and empty

```python
# OK: If None and {} are semantically identical
def add_headers(headers: dict[str, str] | None = None) -> None:
    """Add HTTP headers.

    Args:
        headers: Headers to add. None or {} means no headers.
    """
    headers = headers or {}  # None == {} semantically
    for k, v in headers.items():
        # ...
```

## Common Patterns to Fix

### Pattern 1: Tool parameter coercion

```python
# BAD:
FunctionToolParam(name=t.name, description=t.description or "", parameters=t.inputSchema or {})

# GOOD:
FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema)
```

### Pattern 2: Dictionary merging

```python
# BAD:
merged = {**(base or {}), **(override or {})}

# GOOD:
base_dict = base if base is not None else {}
override_dict = override if override is not None else {}
merged = {**base_dict, **override_dict}
```

### Pattern 3: Iteration over potentially None collections

```python
# BAD:
for item in collection or []:
    process(item)

# GOOD:
if collection is not None:
    for item in collection:
        process(item)

# OR:
for item in collection or []:  # OK if [] and None are semantically same
    process(item)
# But document that None == [] in this context
```

## Validation

```bash
# After fixing:
mypy --strict path/to/file.py

# Should reveal:
# - If defensive default was unnecessary: mypy happy
# - If type annotation was wrong: mypy error (fix upstream or add assert)
```

## Benefits

✅ **Type safety** - Type annotations match runtime behavior
✅ **Explicit intent** - Clear when None is acceptable vs needs conversion
✅ **Catches bugs** - Type checker finds places where None handling is missing
✅ **Better APIs** - Clear contracts about what's required vs optional
✅ **Upstream bug detection** - Find cases where library types are wrong
