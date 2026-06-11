# Scan: Unnecessary Verbosity

## Context

@../shared-context.md

## Overview

Code that is longer than necessary without improving readability, maintainability, or clarity. The goal is conciseness without sacrificing understanding.

## Core Principle

**Eliminate intermediate variables that don't add clarity.** If a variable is assigned once and used once immediately after, it's likely unnecessary unless it significantly improves readability.

## Pattern 1: Single-Assignment Variables

Variables assigned once and used exactly once in the next statement.

### BAD: Unnecessary intermediate variables

```python
# BAD: Three lines where one suffices
provider = DockerFileProvider(container_files)
collector = FileCollector(provider)
return collector.collect_files()

# BAD: Single-use variable adds no clarity
result = calculate_total(items)
return result

# BAD: Temporary for simple transformation
temp = value.strip()
return temp.lower()

# BAD: Intermediate for constructor argument
config = load_config()
processor = DataProcessor(config)
return processor

# BAD: Path variable used once for single method call
out = base_dir / "critique.json"
s = submit_state.result.model_dump_json(indent=2)
out.write_text(s, encoding="utf-8")
print(f"Saved critique JSON: {out}")
```

### GOOD: Direct usage when clear

```python
# GOOD: Single expression, still clear
return FileCollector(DockerFileProvider(container_files)).collect_files()

# GOOD: Direct return
return calculate_total(items)

# GOOD: Chained methods
return value.strip().lower()

# GOOD: Direct construction
return DataProcessor(load_config())

# GOOD: Fully inline if path not needed later
(base_dir / "critique.json").write_text(
    submit_state.result.model_dump_json(indent=2), encoding="utf-8"
)
print(f"Saved critique JSON: {base_dir / 'critique.json'}")

# BETTER: Keep variable if used multiple times (avoids repeating path construction)
critique_path = base_dir / "critique.json"
critique_path.write_text(submit_state.result.model_dump_json(indent=2), encoding="utf-8")
print(f"Saved critique JSON: {critique_path}")

# GOOD: Inline single-use variable in function call
# BAD: Create intermediate variable for single use
async def send_snapshot(self, approval_hub: ApprovalHub) -> None:
    pending = [ApprovalBrief(tool_call=req.tool_call) for req in approval_hub._requests.values()]
    snapshot = ApprovalsSnapshot(pending=pending)
    await self.broadcast(snapshot)

# GOOD: Inline the variable - saves 2 lines, equally clear
async def send_snapshot(self, approval_hub: ApprovalHub) -> None:
    await self.broadcast(
        ApprovalsSnapshot(pending=[ApprovalBrief(tool_call=req.tool_call) for req in approval_hub._requests.values()])
    )

# BAD: Single-use snapshot wrapper (real codebase example)
async def send_snapshot(self, compositor: Compositor) -> None:
    """Send current MCP snapshot to all clients."""
    sampling = await compositor.sampling_snapshot()
    snapshot = McpSnapshot(sampling=sampling)  # ❌ Single-use variable
    await self.broadcast(snapshot)

# GOOD: Direct construction in call
async def send_snapshot(self, compositor: Compositor) -> None:
    """Send current MCP snapshot to all clients."""
    await self.broadcast(
        McpSnapshot(sampling=await compositor.sampling_snapshot())
    )
```

### Subpattern: Redundant Field Storage

Don't store references to sub-fields when you already have the parent object stored.

```python
# BAD: Storing both parent and its fields
class Server:
    def __init__(self, engine: Engine):
        self._engine = engine
        self._agent_id = engine.agent_id  # Redundant
        self._persistence = engine.persistence  # Redundant
        self._docker = engine.docker_client  # Redundant

    async def handle(self, id: str):
        # Creates multiple paths to same dependency
        await self._persistence.get(self._agent_id, id)

# GOOD: Access through parent
class Server:
    def __init__(self, engine: Engine):
        self._engine = engine

    async def handle(self, id: str):
        # Single path to dependency
        await self._engine.persistence.get(self._engine.agent_id, id)
```

**Why this is bad**:

- **Multiple paths**: Creates redundant ways to access same downstream dependency
- **Synchronization risk**: If engine internals change, cached fields become stale
- **Unnecessary fields**: Pollutes class namespace with redundant state
- **Maintenance burden**: More fields to track and understand

**No performance exception**: Python is not for performance-critical code. Don't cache fields for "performance" - the overhead is negligible and the complexity isn't worth it.

### When Intermediate Variables ARE Good

```python
# GOOD: Complex expression needs breakdown
user_permissions = get_user_permissions(user_id)
group_permissions = get_group_permissions(user_groups)
final_permissions = merge_permissions(user_permissions, group_permissions, overrides)
return apply_policy(final_permissions, policy)

# GOOD: Long identifier used multiple times
connection_pool = get_database_connection_pool()
connection_pool.configure(max_size=100)
connection_pool.set_timeout(30)
return connection_pool

# GOOD: Name adds significant semantic meaning
is_eligible_for_discount = (
    customer.is_premium
    and order.total > 100
    and not customer.has_used_discount_this_month
)
if is_eligible_for_discount:
    apply_discount(order)

# GOOD: Breaking up deeply nested expression
base_url = config.get("api", {}).get("endpoints", {}).get("users", {}).get("base")
full_url = f"{base_url}/profile/{user_id}"
return fetch(full_url)
```

## Pattern 2: Verbose Boolean Returns

### BAD: If-else for boolean

```python
# BAD: Unnecessary if-else
def is_valid(self) -> bool:
    if self.value > 0:
        return True
    else:
        return False

# BAD: If with boolean literal
def has_permission(self) -> bool:
    if self.user.is_admin or self.user.id == self.owner_id:
        return True
    return False
```

### GOOD: Direct boolean expression

```python
# GOOD: Direct return
def is_valid(self) -> bool:
    return self.value > 0

# GOOD: Expression is already boolean
def has_permission(self) -> bool:
    return self.user.is_admin or self.user.id == self.owner_id
```

## Pattern 2.5: Redundant Type Annotations with TypeVars

When a function uses TypeVars that are inferred from arguments, explicit type annotations on the result are redundant.

### BAD: Redundant type annotation when TypeVar provides inference

```python
# BAD: Type annotation duplicates what TypeVar already provides
T_Out = TypeVar("T_Out")

async def call_tool_typed(
    session: Client,
    name: str,
    payload: BaseModel,
    out_type: type[T_Out],
) -> T_Out:
    ...

# Type checker already knows result is BaseExecResult from out_type argument
result: BaseExecResult = await call_tool_typed(sess, "exec", payload, BaseExecResult)
```

### GOOD: Let TypeVar inference work

```python
# GOOD: Type inferred from out_type argument
result = await call_tool_typed(sess, "exec", payload, BaseExecResult)
# Type checker knows: result is BaseExecResult
```

**Principle**: If a function signature already provides full type information through TypeVars, don't duplicate it with explicit annotations at call sites. Let type inference work.

## Pattern 2.7: Verbose Default Derivation

When a parameter accepts None to mean "derive a default", use the concise `if not x: x = ...` pattern.

### BAD: Verbose default derivation

```python
# BAD: Separate variable + conditional
def configure(url: str | None = None, token: str | None = None):
    actual_url = url
    if actual_url is None:
        actual_url = os.getenv("API_URL")

    actual_token = token
    if actual_token is None:
        actual_token = os.getenv("API_TOKEN")

    if actual_url is None or actual_token is None:
        raise ValueError("Missing config")

    return Config(url=actual_url, token=actual_token)

# BAD: Ternary for simple None check
def configure(url: str | None = None):
    actual_url = url if url is not None else os.getenv("API_URL")
    return Config(url=actual_url)

# BAD: Nested conditionals
def configure(url: str | None = None):
    if url is None:
        url = os.getenv("API_URL")
        if url is None:
            raise ValueError("Missing URL")
    return Config(url=url)
```

### GOOD: Concise default derivation

```python
# GOOD: Direct reassignment pattern
def configure(url: str | None = None, token: str | None = None):
    if not url:
        url = os.getenv("API_URL")
    if not token:
        token = os.getenv("API_TOKEN")

    if not url or not token:
        raise ValueError("Missing config")

    return Config(url=url, token=token)

# GOOD: Works for any default derivation, not just os.getenv
def process(data: list | None = None, config: Config | None = None):
    if not data:
        data = fetch_default_data()
    if not config:
        config = load_default_config()

    return run_processing(data, config)

# GOOD: Chain multiple fallbacks
def get_api_key(override: str | None = None) -> str:
    key = override
    if not key:
        key = os.getenv("API_KEY")
    if not key:
        key = load_from_keyring("api-key")
    if not key:
        raise ValueError("No API key found")
    return key
```

**Pattern generalization:**

- Parameter accepts None to mean "use default"
- Default is derived (not a constant)
- Pattern: `if not x: x = derive_default()`

**When to use:**

- Environment variable fallback (`os.getenv`)
- Function call fallback (`load_config()`)
- Complex derivation (`compute_from_system_state()`)
- Multiple fallback levels

**When NOT to use:**

- Default is a simple constant → use parameter default value instead:

  ```python
  # GOOD: Simple constant default
  def process(timeout: int = 30):
      ...

  # BAD: Overcomplicated for constant
  def process(timeout: int | None = None):
      if not timeout:
          timeout = 30
  ```

- Empty string/0/False are valid values → use `is None` check instead:

  ```python
  # GOOD: Allow empty string
  def process(prefix: str | None = None):
      if prefix is None:
          prefix = os.getenv("PREFIX", "")

  # BAD: Empty string would be replaced
  def process(prefix: str | None = None):
      if not prefix:  # ❌ Would replace ""
          prefix = os.getenv("PREFIX", "")
  ```

**Detection:**

```bash
# Find verbose None checks with assignment
rg --type py -U 'if.*is None:.*\n.*=.*getenv' --multiline
rg --type py 'if .* is not None else'

# Find potential simplification candidates
rg --type py 'actual_\w+ = \w+'
```

## Pattern 3: Redundant Conditionals

### BAD: Checking what's already guaranteed

```python
# BAD: Redundant None check after walrus
if (result := compute()) is not None:
    if result:  # Redundant - already checked not None
        process(result)

# BAD: Multiple checks for same condition
if user:
    if user:  # Duplicate check
        return user.name

# BAD: Else after return
def get_status(self):
    if self.is_complete:
        return "complete"
    else:  # Unnecessary else
        return "pending"

# BAD: Unnecessary empty-collection check before operations that handle empty naturally
if tool_tasks:
    async with asyncio.TaskGroup() as tg:
        for name, task in tool_tasks.items():
            tg.create_task(handle(name, task))

# BAD: Early return for empty input in aggregation
def sum_values(items):
    if not items:
        return 0
    total = 0
    for item in items:
        total += item
    return total

# BAD: Early return for empty input in map operation
def process_all(things):
    if not things:
        return
    for thing in things:
        process(thing)
```

### GOOD: Minimal necessary checks

```python
# GOOD: Combined check
if result := compute():
    process(result)

# GOOD: Single check
if user:
    return user.name

# GOOD: No else needed
def get_status(self):
    if self.is_complete:
        return "complete"
    return "pending"

# GOOD: Let TaskGroup handle empty dict (it's a no-op)
async with asyncio.TaskGroup() as tg:
    for name, task in tool_tasks.items():
        tg.create_task(handle(name, task))

# GOOD: Natural handling of empty input
def sum_values(items):
    total = 0
    for item in items:
        total += item
    return total

# GOOD: Loop handles empty collection naturally
def process_all(things):
    for thing in things:
        process(thing)
```

### Principle: Prefer Code That Handles All Cases

**Avoid special-casing empty inputs** when the general case already handles them correctly. Operations like loops, TaskGroups, comprehensions, and aggregations naturally handle empty collections without explicit checks. Adding `if not items: return` is:

- **Unnecessary**: The loop/operation is a no-op anyway for empty inputs
- **Verbose**: Adds extra line and indentation
- **Not worth it**: Any "micro-optimization" is negligible in Python

**Write one thing that handles all cases** rather than branching for edge cases that don't need special treatment.

## Pattern 4: Verbose Exception Handling

### BAD: Catch and re-raise

```python
# BAD: Pointless try-except
try:
    result = dangerous_operation()
except Exception:
    raise  # Just re-raising? Don't catch it!

# BAD: Catch to return None
try:
    return get_value()
except KeyError:
    return None
```

### GOOD: Use appropriate patterns

```python
# GOOD: Let exception propagate
result = dangerous_operation()

# GOOD: Use .get() for dicts
return data.get(key)  # Returns None if missing

# GOOD: Only catch if adding context
try:
    return dangerous_operation()
except ValueError as e:
    raise ProcessingError(f"Failed to process {item}") from e
```

## Pattern 5: Verbose Comprehensions

### BAD: Unnecessary intermediate list

```python
# BAD: Two comprehensions where one suffices
temp = [x * 2 for x in items]
result = [y + 1 for y in temp]

# BAD: Loop for simple transformation
result = []
for item in items:
    result.append(item.upper())
```

### GOOD: Single comprehension

```python
# GOOD: Combined transformation
result = [x * 2 + 1 for x in items]

# GOOD: Comprehension
result = [item.upper() for item in items]
```

## Pattern 6: Walrus Operator Opportunities

The walrus operator (`:=`) can eliminate unnecessary intermediate variables when assigning and immediately checking/using a value.

### BAD: Assign-then-check

```python
# BAD: Two lines for assign + check
result = expensive_call()
if result:
    process(result)

# BAD: Assign + check in while loop
line = file.readline()
while line:
    process(line)
    line = file.readline()

# BAD: Assign + check + access
match = pattern.search(text)
if match:
    return match.group(1)

# BAD: Nested assign + check
data = fetch_data()
if data:
    value = data.get('key')
    if value:
        return value
```

### GOOD: Walrus operator

```python
# GOOD: Assign and check in one line
if result := expensive_call():
    process(result)

# GOOD: Assign in while condition
while line := file.readline():
    process(line)

# GOOD: Assign in conditional
if match := pattern.search(text):
    return match.group(1)

# GOOD: Nested walrus
if (data := fetch_data()) and (value := data.get('key')):
    return value
```

### When NOT to use walrus

```python
# BAD: Sacrifices readability for terseness
if (x := compute_long_complex_name_that_explains_what_it_is()) > 0:
    process(x)  # What was x again?

# BETTER: Name adds clarity
result_from_expensive_validation = compute_long_complex_name_that_explains_what_it_is()
if result_from_expensive_validation > 0:
    process(result_from_expensive_validation)

# BAD: Complex expression in walrus
if (result := (transform(data) if validate(data) else fallback())) is not None:
    ...  # Too complex

# BETTER: Break it down
result = transform(data) if validate(data) else fallback()
if result is not None:
    ...
```

## Detection Strategy

**Primary Method**: Manual code reading - read through source files, understand the context, look for verbose patterns. Automated tools find candidates but miss context.

**Automated Preprocessing** (high recall, requires manual verification):

### AST-Based Detection

Build AST analyzers to find candidates:

1. **SingleAssignmentDetector**: Track assignments and usages, find variables assigned once and used once on next line
   - Visit Assign nodes → record variable names and line numbers
   - Visit Name nodes with Load context → record usage line numbers
   - Find where assignment line + 1 == usage line and usage count == 1

2. **WalrusOpportunityDetector**: Find assign-then-check patterns
   - Walk function bodies looking for consecutive statements
   - Pattern: `ast.Assign` followed by `ast.If` or `ast.While`
   - Check if condition references the assigned variable
   - Report line, variable name, pattern type

3. **VerboseBooleanDetector**: Find `if-true-else-false` returns
   - Check functions with single If statement
   - Look for: body has `return True`, orelse has `return False`
   - Check for Constant nodes with boolean values

**Implementation**: Strong coding LLM can reconstruct these from description above. Key is walking AST, tracking variables, checking adjacency.

### Grep Patterns (Quick High-Recall Scan)

These find candidates for manual review - NOT definitive problems:

```bash
# Walrus opportunities
rg --type py -U "(\w+)\s*=\s*[^\n]+\n\s*if\s+\1[:\s]" --multiline
rg --type py -U "(\w+)\s*=\s*[^\n]+\n\s*while\s+\1[:\s]" --multiline

# Verbose boolean returns
rg --type py "if.*:\s*return True\s*(else:\s*return False|return False)" --multiline

# Else after return
rg --type py "return [^\n]+\n\s*else:" --multiline

# Pointless try-except-raise
rg --type py "except[^:]*:\s*raise\s*$" --multiline

# Simple for-append (comprehension candidate)
rg --type py "^\s*for\s+\w+\s+in.*:\s*$" -A1 --multiline | rg "append\("

# Return variable on next line
rg --type py "(\w+)\s*=\s*[^\n]+\n\s*return\s+\1\s*$" --multiline
```

**Critical**: These patterns have false positives. Always read the actual code, understand intent, check if simplification makes sense.

## Context Analysis

**When to keep intermediate variables:**

1. **Complex expressions** - Breaking down improves readability
   - More than 2 levels of nesting
   - More than 60 characters in expression
   - Multiple function calls with unclear purpose

2. **Multiple uses** - Variable used more than once

3. **Debugging/logging** - Variable captured for inspection

   ```python
   result = expensive_computation()
   logger.debug(f"Computation result: {result}")
   return result
   ```

4. **Semantic naming** - Variable name adds significant meaning

   ```python
   # Name explains what the complex expression means
   is_business_day = weekday < 5 and date not in holidays
   ```

5. **Type narrowing** - Helps type checker

   ```python
   user = get_current_user()  # Type narrowed from Optional[User] to User
   if user:
       return user.name  # Type checker knows user is not None
   ```

**When to remove intermediate variables:**

1. **Single assignment + single use** on consecutive lines
2. **Simple transformation** (method call, constructor)
3. **Name adds no semantic value** (e.g., `result`, `temp`, `value`)
4. **Expression is already clear** without the variable

## Fix Strategy

1. **Identify single-use variables** using AST analysis
2. **Check if removal improves or maintains readability**:
   - Is the expression simple? → Remove
   - Does the variable name add meaning? → Keep
   - Would the line become too long (>88 chars)? → Keep
3. **Inline the variable** and remove the assignment
4. **Run tests** to ensure behavior unchanged

## When Verbosity Is Acceptable

- **PEP 8 line length** - Breaking up long expressions
- **Type narrowing** - Helping mypy understand types
- **Debugging** - Keeping variables for inspection
- **Code review** - Explicit steps for clarity
- **Performance** - Avoiding repeated expensive calls

## Benefits

✅ **Fewer lines** - Less code to read and maintain
✅ **Clearer intent** - Direct expression of what's happening
✅ **Reduced noise** - Fewer meaningless variable names
✅ **Better signal-to-noise** - Code that matters stands out

## References

- [PEP 8 - Programming Recommendations](https://peps.python.org/pep-0008/#programming-recommendations)
- [Refactoring: Inline Variable](https://refactoring.com/catalog/inlineVariable.html)
- [Python AST Documentation](https://docs.python.org/3/library/ast.html)
