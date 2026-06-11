# Scan: Suspicious Nullability

## Context

@../shared-context.md

## Pattern Description

Nullability (`T | None`) that is misused, propagated through too many layers, or semantically impossible. Nullability should be handled **exactly once** at the optionality branch point, not nested at N levels.

**Key principle**: Handle None at boundaries, not in business logic. Functions deep in the call stack should work with non-None values.

## Fix Philosophy: Propagate Non-Nullability Upward

**CRITICAL**: When you find `assert x is not None`, the fix is usually NOT to keep the assertion or add type: ignore comments. Instead:

1. **Question the type declaration**: Why is `x` typed as `T | None` in the first place?
2. **Propagate non-nullability upward**: Change the type signature so `x` is `T`, not `T | None`
3. **Handle None at the source**: If a value comes from a nullable source, handle the None case there, then pass non-None values downstream

**Common mistake**: Treating assertions as "necessary for mypy" and leaving nullable types everywhere.

**Correct approach**: Fix the root cause by making the type system reflect reality - if a value is never actually None in practice, it shouldn't be typed as nullable.

**Important**: Suspicious nullability often points to a **design problem**, not just a typing problem. You usually cannot fix it by changing 1-2 annotations. Instead, you may need to:

- Refactor data flow to handle None at boundaries
- Restructure function call chains to eliminate None propagation
- Rethink API design to make optional vs required explicit
- Create type-narrowing helpers or wrapper types

**This is architectural work, not just annotation fixes.**

## Core Principle: Optional at ONE Branch Point, Not Infecting 500 Inner Points

**The Goal**: `Maybe<OneBigOptionalModule>` where module contains `{Foo, Bar, Baz}`

- Optionality exists at **ONE** outer layer
- Once you unwrap the Maybe, everything inside is non-optional
- Handle None **once** at the branch point, then work with non-None values downstream

**The Problem**: `OptionalModule` with `Maybe<Foo>, Maybe<Bar>, Maybe<Baz>`

- Optionality "infects" every single field
- Every function touching Foo must handle None
- Every function touching Bar must handle None
- Every function touching Baz must handle None
- None checks scattered across 500 different points in the codebase

### Haskell Analogy

```haskell
-- GOOD: Optional at one branch point
data Config = Config { host :: String, port :: Int, database :: String }

loadConfig :: IO (Maybe Config)

useConfig :: Config -> IO ()
useConfig cfg = connectToDB (host cfg) (port cfg) (database cfg)
  -- host, port, database are all non-Maybe!

main = do
  maybeConfig <- loadConfig
  case maybeConfig of
    Just config -> useConfig config  -- Handle Maybe ONCE
    Nothing -> putStrLn "No config"

-- BAD: Infecting inner points
data BadConfig = BadConfig
  { host :: Maybe String
  , port :: Maybe Int
  , database :: Maybe String
  }

useBadConfig :: BadConfig -> IO ()
useBadConfig cfg =
  case (host cfg, port cfg, database cfg) of  -- None checks everywhere!
    (Just h, Just p, Just d) -> connectToDB h p d
    _ -> error "Missing config"
```

### Python Translation

```python
# GOOD: Optional at ONE branch point
class DatabaseConfig:
    host: str       # Non-optional!
    port: int       # Non-optional!
    database: str   # Non-optional!

def load_config() -> DatabaseConfig | None:
    """Returns None if config file missing, otherwise complete config."""
    ...

def connect_to_db(config: DatabaseConfig) -> Connection:
    # Zero None checks here! config.host, config.port, config.database all guaranteed non-None
    return Connection(config.host, config.port, config.database)

# Usage: Handle None ONCE
config = load_config()
if config is not None:
    conn = connect_to_db(config)
    # 500 downstream functions work with non-None values
    process_data(conn, config.host)
    validate_schema(conn, config.database)
    # ... no None checks needed in any of these


# BAD: Infecting 500 inner points
class BadDatabaseConfig:
    host: str | None
    port: int | None
    database: str | None

def connect_to_db_bad(config: BadDatabaseConfig) -> Connection:
    # None infection spreads here
    if config.host is None or config.port is None or config.database is None:
        raise ValueError("Missing config")
    return Connection(config.host, config.port, config.database)

def process_data_bad(conn: Connection, host: str | None) -> None:
    # None infection spreads to every function!
    if host is None:
        raise ValueError("Missing host")
    ...

def validate_schema_bad(conn: Connection, database: str | None) -> None:
    # None infection spreads to every function!
    if database is None:
        raise ValueError("Missing database")
    ...

# Usage: None checks at 500+ different points
config = load_bad_config()
conn = connect_to_db_bad(config)  # None check #1
process_data_bad(conn, config.host)  # None check #2
validate_schema_bad(conn, config.database)  # None check #3
# ... 497 more None checks scattered across the codebase
```

**The fix**: Restructure so optionality is handled at the boundary (loading the config), then everything downstream works with complete, non-None values.

### Example: Before (Wrong Approach)

```python
class Container:
    id: str | None  # Docker API says it can be None

def process_container(container: Container) -> None:
    container_id = container.id
    assert container_id is not None  # "Needed for mypy"
    use_container_id(container_id)
```

**Problem**: Accepting nullable type and working around it with assertions.

### Example: After (Propagate Non-Nullability)

```python
class Container:
    id: str | None  # Docker API says it can be None

def _require_container_id(container: Container) -> str:
    """Get container ID after creation (when it's guaranteed non-None)."""
    if container.id is None:
        raise RuntimeError("Container has no ID - must be created first")
    return container.id

def process_container(container: Container) -> None:
    # Propagate non-nullability: container_id is str, not str | None
    container_id = _require_container_id(container)
    use_container_id(container_id)  # No assertion needed!
```

**Better**: Create a type-narrowing helper that returns non-None type, propagating non-nullability to all downstream code.

## Examples of Antipatterns

### BAD: Parameter typed as `T | None` but immediately fails if None

```python
# BAD: Parameter accepts None but immediately raises if it's actually None
def process_user_data(user_id: str | None) -> UserData:
    if user_id is None:
        raise ValueError("user_id is required")  # Why accept None then?
    return fetch_user_data(user_id)

# Usage: Caller has to handle None anyway
user_id = get_optional_user_id()
try:
    data = process_user_data(user_id)  # Might raise!
except ValueError:
    # Handle None case
    pass
```

**Better**:

```python
# GOOD: Don't accept None, handle it at the call site
def process_user_data(user_id: str) -> UserData:
    return fetch_user_data(user_id)

# Usage: Handle None exactly once at the branch point
user_id = get_optional_user_id()
if user_id is not None:
    data = process_user_data(user_id)
else:
    # Handle None case
    pass
```

### BAD: Immediately asserts not None after accessing nullable field

```python
# BAD: Field typed as str | None but we assert it's never actually None
def send_notification(container: Container) -> None:
    container_id = container.id  # Type: str | None from Docker API
    assert container_id is not None, "Container must have an ID"
    # ... use container_id

# Problem: If container.id can never be None after container creation,
# the type is wrong - either Docker API types are overly conservative,
# or we need a type-narrowing helper
```

**Better - Option 1: Type narrowing helper**:

```python
def _require_container_id(container: Container) -> str:
    """Get container ID, raising if None.

    Args:
        container: Docker container object

    Returns:
        Container ID (non-None)

    Raises:
        RuntimeError: If container has no ID (should never happen after creation)
    """
    if container.id is None:
        raise RuntimeError("Container created but has no ID - this should never happen")
    return container.id

def send_notification(container: Container) -> None:
    container_id = _require_container_id(container)  # Type: str
    # ... use container_id
```

**Better - Option 2: Type guard**:

```python
def has_container_id(container: Container) -> TypeGuard[ContainerWithId]:
    return container.id is not None

def send_notification(container: Container) -> None:
    if not has_container_id(container):
        raise RuntimeError("Container has no ID")
    # mypy knows container.id is str here
    container_id = container.id  # Type: str
```

### BAD: Nullability propagated through many layers

```python
# BAD: None propagates through 3+ function layers
def get_user_email(user_id: str | None) -> str | None:
    if user_id is None:
        return None
    user = fetch_user(user_id)
    return user.email if user else None

def format_email_display(email: str | None) -> str | None:
    if email is None:
        return None
    return f"<{email}>"

def send_notification(email: str | None) -> None:
    if email is None:
        return  # Silently skip
    # ... send email

# Usage: None cascades through 4 layers!
user_id = get_optional_user_id()  # str | None
email = get_user_email(user_id)  # str | None
display = format_email_display(email)  # str | None
send_notification(display)  # str | None
```

**Better - Handle None once at branch point**:

```python
# GOOD: Inner functions work with non-None values
def get_user_email(user_id: str) -> str:
    user = fetch_user(user_id)
    return user.email

def format_email_display(email: str) -> str:
    return f"<{email}>"

def send_notification(email: str) -> None:
    # ... send email

# Usage: Handle None ONCE at the optionality branch point
user_id = get_optional_user_id()  # str | None
if user_id is not None:
    email = get_user_email(user_id)  # str
    display = format_email_display(email)  # str
    send_notification(display)  # str
```

### BAD: Semantically impossible None

```python
# BAD: Type says nullable but semantically it can never be None
class User:
    def __init__(self, email: str):
        self.email = email  # Always set in __init__

    def get_email(self) -> str | None:  # Why None? It's always set!
        return self.email

# Problem: Callers have to handle None for no reason
email = user.get_email()  # str | None
if email is not None:  # Pointless check
    send_email(email)
```

**Better**:

```python
class User:
    def __init__(self, email: str):
        self.email = email

    def get_email(self) -> str:  # Never None
        return self.email

# Callers don't need to check
email = user.get_email()  # str
send_email(email)
```

### BAD: Multiple nullable parameters that all must be provided

```python
# BAD: All three parameters are "optional" but function can't work without them
def connect_to_database(
    host: str | None = None,
    port: int | None = None,
    database: str | None = None
) -> Connection:
    if host is None or port is None or database is None:
        raise ValueError("All connection parameters are required")
    return Connection(host, port, database)

# Problem: Why are they optional if they're all required?
```

**Better**:

```python
# GOOD: Required parameters are not nullable
def connect_to_database(
    host: str,
    port: int,
    database: str
) -> Connection:
    return Connection(host, port, database)

# If you want defaults:
def connect_to_database(
    host: str = "localhost",
    port: int = 5432,
    database: str = "mydb"
) -> Connection:
    return Connection(host, port, database)
```

## Detection Strategy

**Goal**: Find ALL instances of suspicious nullability (100% recall target).

**MANDATORY Step 0**: Discover ALL nullable type annotations in the codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL nullable output using your intelligence
- High recall required, high precision NOT required - you determine which are problematic
- Review each nullable for: unnecessary None, None propagation, immediate None checks, assertions
- Prevents lazy analysis by forcing examination of ALL nullable usage

```bash
# Find ALL nullable type annotations with context (union syntax)
rg --type py '\| None' -B 2 -A 1 --line-number

# Find ALL Optional type annotations with context
rg --type py 'Optional\[' -B 2 -A 1 --line-number

# Find Union types that include None
rg --type py 'Union\[.*None' -B 2 -A 1 --line-number

# Count total nullables found
(rg --type py '\| None' && rg --type py 'Optional\[' && rg --type py 'Union\[.*None') | wc -l
```

**What to review for each nullable:**

1. **Immediately fails if None**: Function parameter accepts None but first line raises if None
2. **Immediately asserts not None**: Value assigned from nullable source then `assert x is not None`
3. **None propagation**: Function returns None if param is None (propagating through layers)
4. **Semantically impossible None**: Type says nullable but can never actually be None
5. **Multiple required nullable params**: All params are nullable but all checked and raised together

**Process ALL output**: Read each nullable, use your judgment to identify which match the antipatterns above.

---

**Recall/Precision**: Medium recall (~60-70%) with targeted patterns, requires manual verification

- `grep "| None\).*\n.*if.*is None.*raise"` finds immediate None checks: ~50% recall, ~70% precision
- `grep "assert.*is not None"` finds assertions: ~80% recall, ~60% precision
- AST scan for None propagation patterns: ~40% recall, ~80% precision
- Manual reading required for "semantically impossible None"

**Why medium recall for targeted patterns**:

- Many patterns require understanding control flow across multiple functions
- "Semantically impossible None" requires domain understanding
- None propagation patterns have many variations

**Recommended approach AFTER Step 0**:

1. Run targeted grep/AST patterns to find obvious antipatterns (~60-70% recall, ~60-70% precision)
2. For each candidate, analyze:
   - **Immediately fails if None?** Check first lines of function body
   - **Immediately asserts not None?** Look for `assert x is not None`
   - **Propagates None?** Trace return value through call chain
   - **Semantically impossible?** Understand domain constraints
3. Fix confirmed suspicious nullability
4. **Supplement with manual reading** of nullable parameters and returns from Step 0
5. **Accept**: This pattern requires significant manual analysis

**Medium-recall retrievers**:

### 1. Parameters That Immediately Fail if None

```bash
# Find functions with None parameters that immediately check and raise
# (This is approximate - needs manual verification)

# Find nullable parameters
rg --type py "def \w+\([^)]*: \w+ \| None"

# Then manually check if function starts with:
# if param is None: raise ValueError(...)
```

**AST-based approach**:

```python
# Build tool that finds:
for func in all_functions:
    for param in func.parameters:
        if is_nullable(param.annotation):
            first_statement = func.body[0] if func.body else None
            if is_none_check_that_raises(first_statement, param.name):
                yield Candidate(func, param, reason="Immediately fails if None")
```

### 2. Assertions That Value is Not None

```bash
# Find assertions that value is not None
rg --type py "assert \w+ is not None"

# Find with context to see what's being asserted
rg --type py -B3 "assert \w+ is not None"
```

**High precision**: Most `assert x is not None` indicate suspicious typing

### 3. Nullable Returns That Propagate None

```bash
# Find functions that return None if parameter is None (propagation pattern)
rg --type py -U "def \w+\([^)]*\| None.*\n.*if.*is None.*\n.*return None"

# Find functions returning x if x else None pattern
rg --type py "return \w+ if \w+ else None"
```

### 4. Functions with Multiple None Checks

```bash
# Find functions that check multiple parameters for None
rg --type py -A10 "def \w+\(" | grep -B1 "if.*is None.*or.*is None"
```

### 5. AST-Based Discovery (Comprehensive)

Build tool that analyzes:

```python
# Pseudocode for AST-based detection
for func in all_functions:
    # Pattern 1: Immediately fails if None
    for param in func.nullable_parameters:
        if first_statement_raises_if_none(func, param):
            yield HighPriorityCandidate(
                func, param,
                reason="Parameter accepts None but immediately raises"
            )

    # Pattern 2: Immediately asserts not None
    for assignment in func.assignments:
        if next_statement_is_assert_not_none(assignment):
            yield Candidate(
                func, assignment,
                reason="Immediately asserts not None after assignment"
            )

    # Pattern 3: Propagates None through layers
    if returns_none_if_param_is_none(func):
        callers = find_callers(func)
        if any(caller_also_propagates_none(c) for c in callers):
            yield Candidate(
                func, None,
                reason="Part of None propagation chain (3+ layers)"
            )

    # Pattern 4: Multiple required nullable parameters
    nullable_params = [p for p in func.parameters if is_nullable(p)]
    if len(nullable_params) >= 2:
        if all_checked_and_raised_together(func, nullable_params):
            yield Candidate(
                func, nullable_params,
                reason="Multiple nullable params all required"
            )
```

**Verification for each candidate**:

1. **For "immediately fails if None"**:
   - Read function body: First statement raise if None?
   - Is there ANY code path where None is handled gracefully?
   - If no → Parameter shouldn't be nullable

2. **For "immediately asserts not None"**:
   - Why is this typed as nullable?
   - Is it from external library with conservative typing?
   - Can we create type-narrowing helper?

3. **For "propagates None"**:
   - Trace call chain: How many layers?
   - Where does None originate? (User input? Optional config?)
   - Can we handle None at origin and pass non-None down?

4. **For "semantically impossible None"**:
   - Read class/function semantics
   - Can the value actually be None given the constraints?
   - Example: `user.email` after `User.__init__(email)` sets it

## Investigation Process

### Step 1: Trace the None Source

```python
# Where does None come from?
# - User input? → Validate at input boundary
# - Optional config? → Use default at config load
# - External API? → Parse to non-None model or raise
# - Database query? → Handle "not found" separately from "found"
```

### Step 2: Understand Domain Constraints

```python
# Can this actually be None given the domain?
# Example: Docker container.id after successful creation
# - Docker API types say: str | None
# - Domain knowledge: After creation, always has ID
# - Solution: Type narrowing helper or assertion with explanation
```

### Step 3: Map Call Chain

```python
# How far does None propagate?
def a() -> X | None: ...
def b(x: X | None) -> Y | None: ...
def c(y: Y | None) -> Z | None: ...
def d(z: Z | None) -> None: ...

# Refactor to:
def a() -> X | None: ...  # Only entry point is nullable
def b(x: X) -> Y: ...     # Non-None from here
def c(y: Y) -> Z: ...
def d(z: Z) -> None: ...

# Handle None once:
if x := a():
    d(c(b(x)))
```

### Step 4: Check for Defensive Programming

```python
# Is this defensive programming against bad calls?
def process(required_param: str | None) -> None:
    if required_param is None:
        raise ValueError("required_param is required")
    # ...

# This suggests:
# 1. Parameter is actually required
# 2. Caller might pass None (bad!)
# 3. Solution: Remove | None, let mypy catch bad calls
```

## Fix Strategy

### Fix 1: Remove Nullability from Parameters That Immediately Fail

```python
# Before:
def process(user_id: str | None) -> None:
    if user_id is None:
        raise ValueError("user_id is required")
    # ... process user_id

# After:
def process(user_id: str) -> None:
    # ... process user_id

# Caller handles None:
user_id = get_optional_user_id()
if user_id is not None:
    process(user_id)
```

### Fix 2: Create Type-Narrowing Helper for Assertions

```python
# Before:
container_id = container.id  # str | None
assert container_id is not None, "Container must have ID"
use_container_id(container_id)

# After:
def _require_container_id(container: Container) -> str:
    if container.id is None:
        raise RuntimeError("Container has no ID (should never happen)")
    return container.id

container_id = _require_container_id(container)  # str
use_container_id(container_id)
```

### Fix 3: Eliminate None Propagation

```python
# Before:
def get_user(user_id: str | None) -> User | None:
    if user_id is None:
        return None
    return db.query(User).filter_by(id=user_id).first()

def get_email(user: User | None) -> str | None:
    if user is None:
        return None
    return user.email

def send(email: str | None) -> None:
    if email is None:
        return
    smtp.send(email)

# After:
def get_user(user_id: str) -> User | None:  # Only None if not found
    return db.query(User).filter_by(id=user_id).first()

def get_email(user: User) -> str:
    return user.email

def send(email: str) -> None:
    smtp.send(email)

# Handle at branch point:
user_id = get_optional_user_id()
if user_id is not None:
    user = get_user(user_id)
    if user is not None:  # Actually might not be found
        email = get_email(user)  # Never None
        send(email)  # Never None
```

### Fix 4: Make Required Parameters Non-Nullable

```python
# Before:
def connect(host: str | None, port: int | None, db: str | None) -> Connection:
    if host is None or port is None or db is None:
        raise ValueError("All parameters required")
    return Connection(host, port, db)

# After:
def connect(host: str, port: int, db: str) -> Connection:
    return Connection(host, port, db)

# With defaults if appropriate:
def connect(
    host: str = "localhost",
    port: int = 5432,
    db: str = "mydb"
) -> Connection:
    return Connection(host, port, db)
```

## When Nullability is Justified

Cases where `T | None` is correct:

### 1. Genuinely Optional Data

```python
# User profile where middle name is optional
class User(BaseModel):
    first_name: str
    middle_name: str | None = None  # Genuinely optional
    last_name: str
```

### 2. Database Queries That Might Not Find Results

```python
def find_user_by_email(email: str) -> User | None:
    # None means "not found", which is different from error
    return db.query(User).filter_by(email=email).first()
```

### 3. Caching / Memoization

```python
def get_cached_value(key: str) -> str | None:
    # None means "not in cache", caller will compute and cache
    return cache.get(key)
```

### 4. External API That Can Return None

```python
# Docker container.id is None before container is created
# This is correct typing from the library
# Solution: Type narrowing helper for "after creation" case
```

## Validation

```bash
# After fixing nullability:
mypy --strict path/to/file.py

# Should reveal if we need type narrowing:
# - If mypy happy: nullability was suspicious
# - If mypy complains about None: might need isinstance check or assert
```

## Benefits

✅ **Clearer contracts** - Function signature shows what's truly required
✅ **Fewer None checks** - Business logic works with non-None values
✅ **Better error messages** - Fail at boundary with context, not deep in call stack
✅ **Type safety** - mypy catches missing None handling at call sites
✅ **Easier debugging** - None handled at optionality branch, not scattered everywhere
