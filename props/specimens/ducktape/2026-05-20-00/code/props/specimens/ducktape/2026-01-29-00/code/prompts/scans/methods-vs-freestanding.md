# Scan: Methods vs Freestanding Functions

## Context

@../shared-context.md

## Overview

Code that should be methods (class/instance) but is implemented as freestanding functions. Indicates missing object-oriented structure.

## Pattern 1: Factory Functions Should Be Classmethods

### BAD: Freestanding factory function

```python
# BAD: Factory as freestanding function
@dataclass
class MirrorConfig:
    base_url: str
    token: str

def make_mirror_config(base_url: str | None, token: str | None) -> MirrorConfig:
    """Create config with environment fallback."""
    if not base_url:
        base_url = os.getenv("GITEA_BASE_URL")
    if not token:
        token = os.getenv("GITEA_TOKEN")
    if not base_url or not token:
        raise ValueError("...")
    return MirrorConfig(base_url=base_url, token=token)

# Usage scattered across codebase
config = make_mirror_config(url, token)
```

### GOOD: Factory as classmethod

```python
# GOOD: Factory as classmethod
@dataclass
class MirrorConfig:
    base_url: str
    token: str

    @classmethod
    def resolve(cls, base_url: str | None, token: str | None) -> MirrorConfig:
        """Create config with environment fallback."""
        if not base_url:
            base_url = os.getenv("GITEA_BASE_URL")
        if not token:
            token = os.getenv("GITEA_TOKEN")
        if not base_url or not token:
            raise ValueError("...")
        return cls(base_url=base_url, token=token)

# Clear ownership
config = MirrorConfig.resolve(url, token)
```

**Detection patterns:**

- Function name: `make_X`, `create_X`, `from_X`, `build_X`, `resolve_X`, `parse_X`
- Returns instance of class X
- Located near class X definition

**Why classmethod is better:**

- Clear ownership: method belongs to the class
- Discoverable: IDE shows it when typing `MirrorConfig.`
- Inheritance: Subclasses can override factory behavior
- Namespace: No pollution of module-level namespace

## Pattern 2: Tightly Coupled Functions Should Be Methods

### BAD: Function tightly coupled to class

```python
# BAD: Freestanding function operating on instance
async def record_errors_to_db(
    db: ResponsesDB,
    key: CacheKey,
    response_id: str | None,
    error_reason: str
):
    """Record error using db, key, and response_id."""
    await db.record_error(key, error_reason=error_reason, response_id=response_id, ...)

# Usage
await record_errors_to_db(ctx.db, ctx.key, ctx.response_id, "error")
```

### GOOD: Method on the class

```python
# GOOD: Method operating on instance fields
class StreamingContext:
    def __init__(self, db: ResponsesDB, key: CacheKey):
        self.db = db
        self.key = key
        self.response_id: str | None = None

    async def record_error(self, error_reason: str):
        """Record error using instance state."""
        await self.db.record_error(
            self.key, error_reason=error_reason, response_id=self.response_id, ...
        )

# Usage
await ctx.record_error("error")
```

**Detection heuristics:**

1. Function takes instance/struct as first parameter
2. Accesses 3+ fields from that instance
3. Function name suggests it operates on that type

**Why method is better:**

- Cohesion: Related data and behavior in one place
- Encapsulation: Instance fields are implementation details
- Shorter call sites: No need to pass instance fields individually
- Clear ownership: Method conceptually "belongs to" the class

## Pattern 3: Bundled Parameters Should Become Class

### BAD: Function with bundled parameters

```python
# BAD: Same 4 parameters passed everywhere together
async def _proxy_stream(
    resp: httpx.Response,
    *,
    db: ResponsesDB,
    key: CacheKey,
    response_id: str | None,
    start_time: float
) -> AsyncIterator[bytes]:
    # Uses db, key, response_id repeatedly
    await db.mark_in_progress(key, response_id)
    await db.append_frame(key, frame, response_id=response_id)
    await db.finalize_response(key, response_id=response_id, ...)

async def record_error(db: ResponsesDB, key: CacheKey, response_id: str | None, ...):
    await db.record_error(key, response_id=response_id, ...)

# Call sites pass same bundle repeatedly
await _proxy_stream(resp, db=db, key=key, response_id=None, start_time=t)
await record_error(db=db, key=key, response_id=None, ...)
```

### GOOD: Bundle into class with methods

```python
# GOOD: Parameters bundled into class
class StreamingContext:
    def __init__(self, db: ResponsesDB, key: CacheKey):
        self.db = db
        self.key = key
        self.response_id: str | None = None

    async def proxy_stream(self, resp: httpx.Response, start_time: float) -> AsyncIterator[bytes]:
        # Access bundled state via self
        await self.db.mark_in_progress(self.key, self.response_id)
        await self.db.append_frame(self.key, frame, response_id=self.response_id)
        await self.db.finalize_response(self.key, response_id=self.response_id, ...)

    async def record_error(self, error_reason: str):
        await self.db.record_error(self.key, response_id=self.response_id, ...)

# Call sites are cleaner
ctx = StreamingContext(db=db, key=key)
await ctx.proxy_stream(resp, start_time=t)
await ctx.record_error(...)
```

**Detection heuristics:**

1. 3+ parameters passed together in multiple functions
2. Parameters represent cohesive state (not independent values)
3. Same bundle appears at multiple call sites

**When bundling is appropriate:**

- Parameters are conceptually related (db + key + response_id for database operations)
- Multiple functions operate on the same bundle
- Bundle represents lifecycle state (streaming context, transaction context)

**When NOT to bundle:**

- Parameters are independent configuration values
- Only one function uses the combination
- Bundling would create artificial coupling

## Pattern 4: State-Modifying Functions Should Be Methods

### BAD: Function modifies instance state

```python
# BAD: Freestanding function modifies passed instance
def set_response_id(ctx: StreamingContext, response_id: str):
    """Update context's response_id."""
    ctx.response_id = response_id

# Usage
set_response_id(ctx, "msg_123")
```

### GOOD: Method modifies own state

```python
# GOOD: Method modifies instance state
class StreamingContext:
    def set_response_id(self, response_id: str):
        """Update response_id."""
        self.response_id = response_id

# Usage
ctx.set_response_id("msg_123")
```

**Detection:**

- Function takes mutable instance as parameter
- Primary purpose is modifying that instance's state
- Function name suggests mutation (set_X, update_X, add_X, remove_X)

## Detection Strategy

### Grep Patterns

```bash
# Find factory-pattern functions
rg --type py "^def (make|create|from|build|resolve|parse)_\w+" -A1

# Find functions taking instance as first parameter
rg --type py "^def \w+\(.*: \w+," -A5 | rg "\.\w+\.\w+\.\w+"

# Find repeated parameter bundles (manual review required)
rg --type py "def.*\(.*db.*key.*response_id" --type py
```

### Manual Analysis

1. **Read function signatures** looking for:
   - Factory-pattern names + return type matching class name
   - Instance parameter + multiple field accesses
   - Same parameter combinations appearing repeatedly

2. **Check coupling**:
   - Count field accesses from instance parameter
   - 3+ field accesses → strong coupling → should be method

3. **Analyze call sites**:
   - Same parameter bundle at multiple call sites → bundle into class
   - Creating instance just to call one function → should be classmethod

## Fix Strategy

1. **For factories**:
   - Move function into class as `@classmethod`
   - Change `return ClassName(...)` to `return cls(...)`
   - Update call sites: `make_config(...)` → `Config.make(...)`

2. **For tight coupling**:
   - Move function into class as instance method
   - Replace instance parameter with `self`
   - Change field accesses: `instance.field` → `self.field`
   - Update call sites: `func(instance, ...)` → `instance.func(...)`

3. **For bundled parameters**:
   - Create class to hold bundle
   - Move functions as methods on that class
   - Update call sites to create instance once, call methods multiple times

4. **For state modification**:
   - Move into class as instance method
   - Replace instance parameter with `self`

## Benefits

✅ **Discoverability** - IDE shows methods when typing `instance.`
✅ **Cohesion** - Related data and behavior in one place
✅ **Encapsulation** - Implementation details hidden
✅ **Shorter call sites** - Don't repeat instance fields
✅ **Clear ownership** - Obvious which class owns the behavior
✅ **Inheritance** - Subclasses can override behavior

## When Freestanding Functions ARE Appropriate

- **Pure functions**: No instance state, operates only on parameters
- **Utilities**: Generic operations not tied to specific class
- **Composition**: Combining multiple unrelated classes
- **Top-level orchestration**: High-level workflows

```python
# GOOD: Pure utility function
def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# GOOD: Composition of unrelated types
def sync_database_to_cache(db: Database, cache: RedisCache):
    data = db.fetch_all()
    cache.bulk_set(data)
```

## References

- [Python classmethod docs](https://docs.python.org/3/library/functions.html#classmethod)
- [SOLID: Single Responsibility Principle](https://en.wikipedia.org/wiki/Single-responsibility_principle)
- [Refactoring: Move Method](https://refactoring.com/catalog/moveMethod.html)
