# Scan: Walrus Operator (`:=`) Opportunities

**Goal**: Identify patterns that benefit from the walrus operator - avoiding duplicate computation/lookup while keeping scope tight.

## When Walrus Wins

The walrus operator is valuable when you need to **use a value AND test/process it** in the same expression, avoiding:

- Duplicating expensive computation or lookups
- Polluting outer scope with temporary variables
- Verbose multi-line patterns that obscure intent

### Core Patterns

1. **dict.get() + if check** - Avoid double lookup or unnecessary scope
2. **while loop with fetch** - Read/fetch until exhausted pattern
3. **Duplicate function calls** - Compute once in if condition, use in body
4. **Regex match + use groups** - Match once, use groups if matched
5. **List comprehension filter** - Filter on transformed value, keep transform

**Key insight**: Walrus prevents duplicate work (computation/lookup) while keeping variable scope tight to where it's used.

## Detection Strategy

**MANDATORY Step 0**: Run walrus operator opportunity scans.

- This scan is **required** - do not skip this step
- You **must** read and process ALL walrus candidate output using your intelligence
- High recall required, high precision NOT required (~10-30% precision expected) - you determine which benefit from walrus
- Review each for: variable scope, duplicate work, readability improvement
- Prevents lazy analysis by forcing examination of ALL test-and-use patterns

These scans surface candidates for manual review. High recall, low precision expected (~10-30% precision).

```bash
# 1. dict.get() in if conditions (original pattern)
# High recall for dict.get walrus opportunities
rg --type py 'if.*\.get\(' -B 1 -A 3 --line-number

# 2. Assignment followed by if/while using same variable (up to 3 blank lines between)
# Catches: x = f(); if x: ... → if (x := f()): ...
rg --type py -U '\w+ = .+(\n\s*){1,3}(if|while) \w+' -B 1 -A 3 --line-number

# 3. re.match/search/findall in if conditions
# Catches: if re.match(...): m = re.match(...) → if (m := re.match(...)): ...
rg --type py 'if.*\bre\.(match|search|findall)\(' -B 2 -A 3 --line-number

# 4. List comprehensions with if clause
# Manual review for: [f(x) for x in items if f(x)] → [y for x in items if (y := f(x))]
rg --type py '\[.+ for .+ in .+ if .+\]' -B 1 -A 1 --line-number

# 5. While loops (check for pre-fetch pattern)
# Catches: line = f.read(); while line: ... → while (line := f.read()): ...
rg --type py '^[[:space:]]*while ' -B 2 -A 2 --line-number
```

**Manual verification process**:

1. **Check variable usage**: Is variable ONLY used in the conditional block?
2. **Verify pattern**: Assignment followed by test (if/while) using that variable?
3. **Check for duplicate calls**: Same function called in condition and body?
4. **Apply walrus**: Move assignment into conditional with `:=`
5. **Readability check**: Does walrus make intent clearer or obscure it?

## Pattern Examples

### Pattern 1: dict.get() + if check

**Example 1a: Positive check**

```python
# Before
p = child_env.get(key)
if p:
    Path(p).mkdir(parents=True, exist_ok=True)

# After - walrus
if (p := child_env.get(key)):
    Path(p).mkdir(parents=True, exist_ok=True)
```

**Benefits**: Saves one line, clearer intent, variable scoped to block.

### Example 2: None check (good candidate)

**File**: `mcp_infra/compositor/server.py:196`

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

### Pattern 2: while loop with fetch/read

```python
# Before - pre-fetch then loop
line = file.readline()
while line:
    process(line)
    line = file.readline()

# After - walrus
while (line := file.readline()):
    process(line)
```

**Benefits**: Eliminates duplicate readline logic, clearer "read until exhausted" pattern.

### Pattern 3: Duplicate function call in if

```python
# Before - compute twice
if expensive_computation(x) is not None:
    result = expensive_computation(x)  # Wasteful!
    use(result)

# After - walrus
if (result := expensive_computation(x)) is not None:
    use(result)
```

**Benefits**: Avoids duplicate computation, clearer that we're testing then using the result.

### Pattern 4: Regex match then use

```python
# Before - match twice
if re.match(r"(\d+)-(\d+)", text):
    m = re.match(r"(\d+)-(\d+)", text)  # Duplicate match!
    start, end = m.groups()
    process(start, end)

# After - walrus
if (m := re.match(r"(\d+)-(\d+)", text)):
    start, end = m.groups()
    process(start, end)
```

**Benefits**: Avoids duplicate regex execution, clearer match-and-use pattern.

### Pattern 5: List comprehension with filter on transform

```python
# Before - transform twice per item
configs = [parse_config(p) for p in paths if parse_config(p) is not None]
# Calls parse_config() TWICE for each path!

# After - walrus
configs = [cfg for p in paths if (cfg := parse_config(p)) is not None]
```

**Benefits**: Avoids duplicate parse_config calls, much more efficient for expensive transforms.

### Example 4: Multi-use variable (skip)

**File**: `rspcache/models.py:56`

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

## When NOT to Apply Walrus

❌ **Variable used outside the conditional block**

```python
# BAD: value needed outside the block
if (value := data.get("key")):
    process(value)
log(value)  # NameError! value not in scope
```

❌ **Multiple checks on same variable**

```python
# BAD: variable tested multiple times
value = data.get("key")
if value is None:
    return default
if not validate(value):
    return fallback
return value
```

❌ **Hurts readability with complex nesting**

```python
# BAD: Too complex
if (x := a()) and (y := b(x)) and (z := c(y)) and z > 10:
    ...

# GOOD: Multi-line for clarity
x = a()
y = b(x)
z = c(y)
if z > 10:
    ...
```

❌ **Value used only once in simple context**

```python
# BAD: Unnecessary walrus
if (x := compute()):
    return x

# GOOD: Just return directly
return compute()
```

## Detection Summary: High-Recall Patterns

These patterns can be detected with high recall (>80%) using the grep commands above:

✅ **dict.get() + if/while** - Pattern: `if x.get(` or `x = d.get(); if x:`
✅ **while with pre-fetch** - Pattern: `x = f(); while x:`
✅ **re.match/search in if** - Pattern: `if re.match(` then body uses match
✅ **Assignment + if/while** - Pattern: `x = compute(); if x:` or `while x:`
⚠️ **List comprehension** - Pattern: `[f(x) for x in ... if f(x)]` - needs manual inspection

**Expected precision**: 10-30% (many false positives, but that's okay - agent reviews all candidates)

**Key to identify true positives**:

1. Variable assigned, then immediately tested in if/while
2. Variable only used within the conditional block
3. Same computation appears in condition and body (duplicate work)
4. No usage of variable outside the conditional scope

## Application Process

1. **Run high-recall scans** (commands in Detection Strategy section above)
2. **For each candidate**, verify:
   - Is variable ONLY used in the immediate conditional block?
   - No `else` clause using the variable outside its block?
   - Pattern matches one of: `if var:`, `if not var:`, `if var is None:`, `while var:`
   - Or: duplicate function call in condition and body?
3. **Apply transformation**:
   - Move assignment into conditional: `if (var := expr):`
   - Add parentheses around assignment (required by Python syntax)
   - Verify with formatter/linter
4. **Test**: Behavior unchanged (variable scope change shouldn't matter)

## Common False Positives

When reviewing scan results, these are NOT walrus candidates:

- **Chained .get()**: `a.get("x").get("y")` - different pattern
- **Variable used in multiple branches**: Needs outer scope
- **Complex boolean logic**: Multiple variables, walrus hurts readability
- **Default value with get()**: `dict.get(key, default)` - already concise

## Notes

- Python 3.8+ required for walrus operator
- Pre-commit formatters (black, ruff) handle parentheses correctly
- Walrus is controversial - use for simple get-and-check only
- Main benefit: reduces variable scope, prevents accidental reuse
- Secondary benefit: one less line of code
