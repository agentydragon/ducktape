# Scan: Prefer Functional Patterns Over Imperative Loops

## Context

@../shared-context.md

## Pattern Description

Prefer functional patterns (list comprehensions, generator expressions, map/filter) over imperative loops with append/extend when building collections. Functional patterns are more concise, often faster, and communicate intent more clearly.

**Key principle**: If you're just transforming or filtering items into a new list, use a comprehension or generator expression instead of a loop with append.

## Examples of Antipatterns

### BAD: Loop with append to build list

```python
# BAD: Imperative loop
tools_payload: list[FunctionToolParam] = []
for t in tools:
    tools_payload.append(
        FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema)
    )
```

**Why this is bad**:

- More verbose (4 lines vs 1-3 lines)
- Communicates "do these steps" instead of "build this list from that"
- Slightly slower (repeated append calls vs pre-allocated list)
- More mutable state (need to initialize empty list)

**Better - List comprehension**:

```python
# GOOD: Functional pattern
tools_payload = [
    FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema)
    for t in tools
]
```

**Better - Generator expression with extend**:

```python
# GOOD: If tools_payload already exists and you're adding to it
tools_payload.extend(
    FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema)
    for t in tools
)
```

### BAD: Loop with append for filtering

```python
# BAD: Imperative filtering
enabled = []
for item in items:
    if item.enabled:
        enabled.append(item)
```

**Better - Filter with comprehension**:

```python
# GOOD: Functional pattern
enabled = [item for item in items if item.enabled]
```

### BAD: Loop with conditional append

```python
# BAD: Building list with transformation
results = []
for x in values:
    if x > 0:
        results.append(x * 2)
```

**Better - Comprehension with condition**:

```python
# GOOD: Functional pattern
results = [x * 2 for x in values if x > 0]
```

### BAD: Nested loops with append

```python
# BAD: Imperative nested loops
pairs = []
for x in xs:
    for y in ys:
        pairs.append((x, y))
```

**Better - Nested comprehension**:

```python
# GOOD: Functional pattern
pairs = [(x, y) for x in xs for y in ys]
```

### BAD: Loop to extract field

```python
# BAD: Extracting single field
names = []
for user in users:
    names.append(user.name)
```

**Better - Comprehension or map**:

```python
# GOOD: Comprehension
names = [user.name for user in users]

# ALSO GOOD: map (for very simple cases)
names = list(map(lambda u: u.name, users))
```

### BAD: Loop with transformation and filtering

```python
# BAD: Multiple operations in loop
active_ids = []
for item in items:
    if item.is_active:
        active_ids.append(item.id)
```

**Better - Comprehension**:

```python
# GOOD: Single expression
active_ids = [item.id for item in items if item.is_active]
```

## Detection Strategy

**MANDATORY Step 0**: Discover ALL for-loops and .append usage in the codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL for-loop and .append output using your intelligence
- High recall required, high precision NOT required - you determine which are imperative antipatterns
- Review each for: simple transformation/filtering only, no side effects, fits in 1-2 lines
- Prevents lazy analysis by forcing examination of ALL loop-based list building

```bash
# Find ALL for-loops (with up to 10 lines of context to see body)
rg --type py '^[[:space:]]*for ' -A 10 --line-number

# Find ALL .append usage with context
rg --type py '\.append\(' -B 1 -A 1 --line-number

# Find ALL .extend usage with context (less common but worth checking)
rg --type py '\.extend\(' -B 1 -A 1 --line-number
```

**What to review for each for-loop + .append**:

1. **Is it just building a list?** Only transformation/filtering, no side effects
2. **Are there side effects?** I/O operations, mutations, calls with side effects
3. **Is the logic simple enough?** Fits in 1-2 lines as a comprehension
4. **Early termination?** Has break/continue with complex conditions
5. **Multiple collections?** Building multiple lists/dicts in same loop

**Process ALL output**: Read each loop, use your judgment to identify simple transformation patterns.

---

**Goal**: Find imperative loops that should be functional patterns (high recall ~85%).

**Recall/Precision**: High recall (~85%) with automation, medium precision (~70%)

- Pattern: `for ... in ...: \n ... .append(` finds most cases
- False positives: Loops with side effects, complex logic

**Why high recall**:

- Pattern is syntactically distinct
- Easy to identify loop + append/extend
- Most cases are simple transformations

**Recommended approach AFTER Step 0**:

1. Search for loops with append: `for .* in .*:\n.*\.append\(`
2. For each candidate, check:
   - **Is it just building a list?** (transformation/filtering only)
   - **Are there side effects?** (I/O, mutations, calls with side effects)
   - **Is the logic simple enough?** (fits in 1-2 lines)
3. If yes to question 1 and no to questions 2-3, refactor to comprehension
4. Accept ~15% false positives for complex loops with legitimate side effects

**High-recall retriever**:

```bash
# Find loops with append
rg --type py -A 2 "for .* in .*:" | grep -B 1 "\.append\("

# Find loops with extend (less common but worth checking)
rg --type py -A 2 "for .* in .*:" | grep -B 1 "\.extend\("

# More precise: multi-line search for simple append pattern
rg --type py --multiline "for \w+ in [^:]+:\n\s+\w+\.append\("
```

**Verification for each candidate**:

1. **Check for side effects**:

   ```python
   # Side effects = NOT a candidate
   for item in items:
       log.debug(f"Processing {item}")  # I/O
       item.process()  # Mutation
       results.append(item.value)
   ```

2. **Check complexity**:

   ```python
   # Too complex = NOT a candidate
   for item in items:
       if item.type == "A":
           value = calculate_a(item)
       elif item.type == "B":
           value = calculate_b(item)
       else:
           value = None
       results.append(value)
   ```

3. **Simple transformation = GOOD candidate**:

   ```python
   # Simple = GOOD candidate
   for item in items:
       results.append(item.name.upper())

   # Refactor to:
   results = [item.name.upper() for item in items]
   ```

## Fix Strategy

### Fix 1: Simple transformation → list comprehension

```python
# Before:
items = []
for x in values:
    items.append(transform(x))

# After:
items = [transform(x) for x in values]
```

### Fix 2: Filter and transform → comprehension with condition

```python
# Before:
results = []
for x in values:
    if predicate(x):
        results.append(transform(x))

# After:
results = [transform(x) for x in values if predicate(x)]
```

### Fix 3: Extending existing list → generator with extend

```python
# Before:
for x in new_values:
    existing_list.append(transform(x))

# After:
existing_list.extend(transform(x) for x in new_values)
```

### Fix 4: Building dict → dict comprehension

```python
# Before:
mapping = {}
for item in items:
    mapping[item.key] = item.value

# After:
mapping = {item.key: item.value for item in items}
```

### Fix 5: Building set → set comprehension

```python
# Before:
unique = set()
for item in items:
    unique.add(item.id)

# After:
unique = {item.id for item in items}
```

## When Imperative Loops Are Acceptable

### 1. Side effects required

```python
# OK: Has side effects (logging, mutation)
for item in items:
    logger.info(f"Processing {item}")
    item.mark_processed()
    results.append(item.result)
```

### 2. Complex logic doesn't fit comprehension

```python
# OK: Too complex for comprehension
for item in items:
    if item.type == "special":
        result = special_handler(item)
        if result.is_valid:
            results.append(result)
    else:
        results.append(default_handler(item))
```

### 3. Early termination needed

```python
# OK: Need to break early
for item in items:
    if item.is_critical_error():
        break
    results.append(item.process())
```

### 4. Multiple collections being built

```python
# OK: Building multiple lists (though could use zip/separate comprehensions)
successes = []
failures = []
for item in items:
    if item.is_valid():
        successes.append(item)
    else:
        failures.append(item)
```

### 5. Accumulator pattern with state

```python
# OK: Stateful accumulation
total = 0
results = []
for value in values:
    total += value
    results.append((value, total))  # Running sum
```

## Performance Considerations

List comprehensions are generally faster than loops with append:

```python
# Slower: Loop with append
items = []
for i in range(1000):
    items.append(i * 2)

# Faster: List comprehension (~30% faster)
items = [i * 2 for i in range(1000)]
```

**Why comprehensions are faster**:

- Pre-allocate list size when possible
- Reduce Python bytecode overhead
- Less attribute lookups (no repeated `.append`)

Generator expressions with extend are also efficient:

```python
# Good: Generator expression (lazy, memory efficient)
items.extend(i * 2 for i in range(1000))
```

## Common Patterns

### Pattern 1: Transform all items

```python
# Before: for + append
results = []
for item in items:
    results.append(item.transform())

# After: comprehension
results = [item.transform() for item in items]
```

### Pattern 2: Filter then transform

```python
# Before: for + if + append
results = []
for item in items:
    if item.is_valid:
        results.append(item.value)

# After: comprehension with filter
results = [item.value for item in items if item.is_valid]
```

### Pattern 3: Flatten nested structure

```python
# Before: nested loop + append
flat = []
for group in groups:
    for item in group.items:
        flat.append(item)

# After: nested comprehension
flat = [item for group in groups for item in group.items]
```

### Pattern 4: Extract attribute

```python
# Before: for + append
names = []
for user in users:
    names.append(user.name)

# After: comprehension
names = [user.name for user in users]
```

### Pattern 5: Build mapping

```python
# Before: for + dict assignment
lookup = {}
for item in items:
    lookup[item.id] = item

# After: dict comprehension
lookup = {item.id: item for item in items}
```

## Validation

After refactoring, ensure:

1. ✅ Same result produced
2. ✅ No side effects lost
3. ✅ No early termination lost
4. ✅ Type checker still passes
5. ✅ Tests still pass

## Benefits

✅ **Conciseness** - 1-3 lines instead of 3-5 lines
✅ **Clarity** - Intent is clearer ("build list from X")
✅ **Performance** - Often faster (pre-allocation, less overhead)
✅ **Immutability** - Less mutable state (no empty list initialization)
✅ **Pythonic** - Idiomatic Python style
✅ **Type inference** - Better for type checkers

## Anti-Benefits (When NOT to Use)

❌ **Complex logic** - If logic doesn't fit 1-2 lines, keep loop
❌ **Side effects** - If loop has I/O, mutations, use imperative
❌ **Multiple outputs** - If building multiple collections, consider keeping loop
❌ **Early termination** - If need to break/continue with complex conditions
❌ **Debugging** - If you need to set breakpoints mid-loop

## Examples from Real Code

### Example 1: Building tool parameters (from agent.py)

```python
# Before:
tools_payload: list[FunctionToolParam] = []
for t in tools:
    tools_payload.append(
        FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema)
    )

# After:
tools_payload = [
    FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema)
    for t in tools
]
```

### Example 2: Filtering active items

```python
# Before:
active = []
for item in items:
    if item.is_active:
        active.append(item)

# After:
active = [item for item in items if item.is_active]
```

### Example 3: Extracting and transforming

```python
# Before:
ids = []
for user in users:
    ids.append(str(user.id))

# After:
ids = [str(user.id) for user in users]
```

## Summary

**Use comprehensions when**:

- ✅ Building a new collection
- ✅ Simple transformation/filtering
- ✅ No side effects
- ✅ Logic fits 1-2 lines

**Use loops when**:

- ❌ Side effects required
- ❌ Complex logic
- ❌ Early termination needed
- ❌ Multiple collections being built
- ❌ Debugging required
