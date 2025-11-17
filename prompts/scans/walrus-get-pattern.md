# Scan: Walrus Operator for `.get()` Patterns

**Goal**: Identify patterns where dictionary `.get()` is immediately followed by a conditional check, which could use the walrus operator (`:=`) for more concise code.

**Detection Strategy**:

1. **Pattern to find**:
   ```bash
   rg -U -A 2 '^\s+(\w+) = (\w+)\.get\(' <paths>
   ```

2. **Manual verification**: Look for these specific patterns:

   **Pattern A: Positive check**
   ```python
   # Current
   value = dict.get(key)
   if value:
       use(value)

   # Walrus
   if (value := dict.get(key)):
       use(value)
   ```

   **Pattern B: Negative check**
   ```python
   # Current
   value = dict.get(key)
   if value is None:
       handle_missing()

   # Walrus
   if (value := dict.get(key)) is None:
       handle_missing()
   ```

   **Pattern C: Not operator**
   ```python
   # Current
   value = dict.get(key)
   if not value:
       handle_missing()

   # Walrus
   if not (value := dict.get(key)):
       handle_missing()
   ```

3. **Grep for continuation patterns**:
   ```bash
   # Find assignment + if check within 2 lines
   rg -U '(\w+) = (\w+)\.get\([^)]+\)\s*(#[^\n]*)?\n\s*if \1' <paths>
   ```

## Examples

### Example 1: Positive check (good candidate)
**File**: `adgn/src/adgn/llm/sandboxer.py:291`

```python
# Before
p = child_env.get(key)
if p:
    Path(p).mkdir(parents=True, exist_ok=True)

# After
if (p := child_env.get(key)):
    Path(p).mkdir(parents=True, exist_ok=True)
```

**Benefits**: Saves one line, clearer intent, variable scoped to block.

### Example 2: None check (good candidate)
**File**: `adgn/src/adgn/mcp/compositor/server.py:196`

```python
# Before
entry = per_name.get(nm)
if entry is None:
    continue

# After
if (entry := per_name.get(nm)) is None:
    continue
```

**Benefits**: Saves one line, clearer that entry is only used in condition.

### Example 3: Class/type check (good candidate)
**File**: `claude/claude_hooks/claude_hooks/inputs.py:67`

```python
# Before
tool_class = TOOL_INPUT_MAP.get(tool_name)
if tool_class:
    # Parse directly with the correct class based on tool_name
    return tool_class.model_validate(tool_input)

# After
if (tool_class := TOOL_INPUT_MAP.get(tool_name)):
    # Parse directly with the correct class based on tool_name
    return tool_class.model_validate(tool_input)
```

**Benefits**: Saves one line, emphasizes get-and-check pattern.

### Example 4: Multi-use variable (skip)
**File**: `adgn/src/adgn/rspcache/models.py:56`

```python
# Keep as-is - variable used in multiple branches
response_id = payload.get("response_id")
if isinstance(response_id, str):
    return response_id
response = payload.get("response")
if isinstance(response, Mapping):
    value = response.get("id")
    if isinstance(value, str):
        return value
```

**Reason**: `response_id` check is not the only logic path, walrus would not help.

### Example 5: Complex condition (skip)
```python
# Keep as-is - walrus would reduce readability
tmp_hint = env_set.get("TMPDIR") or env_set.get("TMP") or env_set.get("TEMP")
home_dir = env_set.get("HOME") or os.environ.get("HOME")
if tmp_hint:
    base = Path(tmp_hint)
```

**Reason**: Multiple `.get()` calls with fallbacks, walrus adds no clarity.

## When NOT to apply

1. **Variable used outside the conditional block**:
   ```python
   value = data.get("key", default)
   if value:
       process(value)
   log(value)  # Used outside the block
   ```

2. **Multiple checks on same variable**:
   ```python
   value = data.get("key")
   if value is None:
       return default
   if not validate(value):
       return fallback
   return value
   ```

3. **Default value specified**:
   ```python
   # This is already concise
   value = data.get("key", default_value)
   if value:
       ...
   ```
   (Though walrus still works: `if (value := data.get("key", default_value)):`)

4. **Complex boolean logic**:
   ```python
   # Keep as-is for readability
   x = dict1.get("a")
   y = dict2.get("b")
   if x and y and other_condition:
       ...
   ```

## Conversion Process

1. **Search for candidates**:
   ```bash
   rg -U -A 2 '^\s+(\w+) = (\w+)\.get\(' adgn/src claude/claude_hooks llm/ducktape_llm_common
   ```

2. **For each candidate**:
   - Check if variable is ONLY used in the immediate conditional
   - Verify there's no `else` clause that also uses the variable outside its block
   - Ensure the pattern is one of: `if var:`, `if not var:`, `if var is None:`, `if var is not None:`

3. **Apply transformation**:
   - Move assignment into the conditional with walrus `:=`
   - Add parentheses around the assignment
   - Verify with linter/formatter that syntax is correct

4. **Test**: Ensure behavior is unchanged (variable scope change shouldn't matter if only used in the block)

## False Positives

- **Chained .get() calls**: `a.get("x").get("y")` - not the pattern
- **Method calls named get()**: `parser.get()` - different semantic
- **Attribute access after .get()**: `obj = dict.get("key"); obj.attr` - needs the variable
- **`.get()` with complex default**: `dict.get(key, expensive_default())` - already reasonable

## Notes

- Python 3.8+ required for walrus operator
- Pre-commit formatters (black, ruff) handle parentheses correctly
- Walrus is controversial - use for simple get-and-check only
- Main benefit: reduces variable scope, prevents accidental reuse
- Secondary benefit: one less line of code
