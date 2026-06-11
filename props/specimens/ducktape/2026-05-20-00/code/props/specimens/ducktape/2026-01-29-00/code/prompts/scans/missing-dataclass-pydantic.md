# Scan: Classes That Should Be Dataclasses or Pydantic Models

## Context

@../shared-context.md

## Pattern Description

Plain classes with boilerplate `__init__` methods that just assign parameters to instance variables, when they should use `@dataclass` or Pydantic `BaseModel` for automatic field handling.

## Core Principle

**If your `__init__` just does `self.x = x` for every parameter, you should use `@dataclass` or Pydantic.**

## When to Use Each

### Use `@dataclass` when

- Simple data container without validation/serialization needs
- No need for JSON/dict conversion
- Internal-only data structures
- Prefer immutability (`frozen=True`)
- Need `__post_init__` for derived fields

### Use Pydantic `BaseModel` when

- Need validation (type checking, constraints, custom validators)
- Need JSON/dict serialization (`model_dump`, `model_validate`)
- API request/response models
- Configuration loading (env vars, files)
- Need schema generation (OpenAPI, JSON Schema)
- Crossing serialization boundaries

### Use plain class when

- Complex initialization logic (transformations, validation, setup)
- Inheritance hierarchies with complex `__init__` chains
- Stateful objects with lifecycle methods
- Need precise control over construction

## Examples

### Pattern 1: Boilerplate `__init__` → `@dataclass`

```python
# BAD: Boilerplate parameter assignment
class LocalAgentRuntime:
    """Runtime for local agent execution."""

    def __init__(
        self,
        running: RunningInfrastructure,
        model: str,
        client_factory: Callable[[str], OpenAIModelProto],
        system_override: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_summary: ReasoningSummary | None = None,
        parallel_tool_calls: bool = True,
        extra_handlers: Iterable[BaseHandler] | None = None,
        ui_bus = None,
        connection_manager = None,
    ):
        self.running = running
        self.model = model
        self._client_factory = client_factory
        self._system_override = system_override
        self._reasoning_effort = reasoning_effort
        self._reasoning_summary = reasoning_summary
        self._parallel_tool_calls = parallel_tool_calls
        self._extra_handlers = list(extra_handlers or [])
        self._ui_bus = ui_bus
        self._connection_manager = connection_manager

        # Initialized later
        self.session: AgentSession | None = None
        self.agent: MiniCodex | None = None

    async def start(self) -> None:
        # Complex initialization logic here
        ...

# GOOD: Dataclass with __post_init__
from dataclasses import dataclass, field

@dataclass
class LocalAgentRuntime:
    """Runtime for local agent execution."""
    running: RunningInfrastructure
    model: str
    client_factory: Callable[[str], OpenAIModelProto]
    system_override: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    reasoning_summary: ReasoningSummary | None = None
    parallel_tool_calls: bool = True
    extra_handlers: list[BaseHandler] = field(default_factory=list)
    ui_bus: Any = None  # Type properly if possible
    connection_manager: Any = None

    # Initialized by start()
    session: AgentSession | None = field(default=None, init=False)
    agent: MiniCodex | None = field(default=None, init=False)

    def __post_init__(self):
        # Handle mutable defaults and transformations
        if self.extra_handlers is None:
            self.extra_handlers = []

    async def start(self) -> None:
        # Complex initialization logic here
        ...
```

**Benefits**:

- Eliminates 11 lines of boilerplate `self.x = x`
- Auto-generates `__repr__`, `__eq__`, `__hash__` (if needed)
- Fields are clearly declared at class level
- Type hints are preserved
- Can use `frozen=True` for immutability

### Pattern 2: Config/Settings Class → Pydantic

```python
# BAD: Manual config loading
class ServerConfig:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8080,
        debug: bool = False,
        timeout: float = 30.0,
    ):
        self.host = host
        if port < 1 or port > 65535:
            raise ValueError("Invalid port")
        self.port = port
        self.debug = debug
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
        self.timeout = timeout

# GOOD: Pydantic with built-in validation
from pydantic import BaseModel, Field

class ServerConfig(BaseModel):
    host: str = "localhost"
    port: int = Field(default=8080, ge=1, le=65535)
    debug: bool = False
    timeout: float = Field(default=30.0, gt=0)

    # Load from dict, JSON, env vars
    @classmethod
    def from_env(cls):
        return cls.model_validate({
            "host": os.getenv("SERVER_HOST", "localhost"),
            "port": int(os.getenv("SERVER_PORT", "8080")),
            "debug": os.getenv("DEBUG", "false").lower() == "true",
            "timeout": float(os.getenv("TIMEOUT", "30.0")),
        })
```

**Benefits**:

- Automatic validation (port range, timeout > 0)
- JSON/dict serialization for free
- Environment variable loading
- Type coercion (string → int/bool/float)
- Clear constraints in field definitions

### Pattern 3: Data Transfer Object → Pydantic

```python
# BAD: Manual serialization in plain class
class UserData:
    def __init__(self, user_id: str, email: str, created: datetime):
        self.user_id = user_id
        self.email = email
        self.created = created

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "created": self.created.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserData:
        return cls(
            user_id=data["user_id"],
            email=data["email"],
            created=datetime.fromisoformat(data["created"]),
        )

# GOOD: Pydantic handles serialization
from pydantic import BaseModel

class UserData(BaseModel):
    user_id: str
    email: str
    created: datetime

    # Use it:
    # user.model_dump(mode="json")  # Auto ISO format
    # UserData.model_validate(data)  # Auto parse datetime
```

**Benefits**:

- No manual `to_dict`/`from_dict` methods
- Datetime serialization/parsing automatic
- Validation on construction
- JSON Schema generation
- FastAPI integration

### Pattern 4: Immutable Value Object → `@dataclass(frozen=True)`

```python
# BAD: Plain class for immutable value
class CacheKey:
    def __init__(self, namespace: str, key: str):
        self.namespace = namespace
        self.key = key

    def __repr__(self) -> str:
        return f"CacheKey(namespace={self.namespace!r}, key={self.key!r})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, CacheKey):
            return NotImplemented
        return self.namespace == other.namespace and self.key == other.key

    def __hash__(self) -> int:
        return hash((self.namespace, self.key))

# GOOD: Frozen dataclass
from dataclasses import dataclass

@dataclass(frozen=True)
class CacheKey:
    namespace: str
    key: str
    # Gets __repr__, __eq__, __hash__ for free!
```

**Benefits**:

- Immutability enforced by `frozen=True`
- Auto-generated `__repr__`, `__eq__`, `__hash__`
- Hashable (can use as dict key, in sets)
- 16 lines → 4 lines

### Pattern 5: When Plain Class Is Correct

```python
# GOOD: Complex initialization logic - plain class is appropriate
class DatabaseConnection:
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        credentials: Credentials,
    ):
        # Complex validation
        self._validate_host(host)

        # Transformation
        self.connection_string = self._build_connection_string(
            host, port, database
        )

        # Setup/initialization
        self._pool = self._create_connection_pool()
        self._lock = asyncio.Lock()

        # Derived state
        self._connection: Connection | None = None
        self._is_healthy = False

    def _validate_host(self, host: str) -> None:
        # Complex validation logic
        ...

    def _build_connection_string(self, ...) -> str:
        # Transformation logic
        ...
```

**When plain class is OK**:

- Parameters undergo complex transformations
- Need to set up resources (locks, pools, connections)
- Complex validation logic
- Derive multiple fields from inputs
- Stateful lifecycle management

## Detection Strategy

**MANDATORY Step 0**: Discover ALL existing dataclasses and review their field definitions.

- This scan is **required** - do not skip this step
- You **must** read and handle ALL dataclass output
- Review every dataclass for: mutable defaults, field ordering, missing field(), incorrect types
- Prevents lazy analysis by forcing examination of ALL existing dataclass usage

```bash
# Find ALL @dataclass declarations with their field definitions (next ~20 lines)
rg --type py '@dataclass' -A 20 --line-number

# Count total dataclasses found
rg --type py '@dataclass' | wc -l
```

**What to review for each dataclass:**

1. **Mutable defaults (BUG)**: `list[T] = []`, `dict[K,V] = {}`, `set[T] = set()` without `field(default_factory=...)`
2. **Field ordering**: Non-default fields must come before fields with defaults
3. **Missing field()**: Mutable defaults, `init=False` fields, `repr=False` fields
4. **Type accuracy**: Are field types correct and specific?
5. **Unnecessary dataclass**: Does this have complex `__post_init__` that suggests plain class is better?

**MANDATORY Step 1**: Run `scan_dataclass_candidates.py` and process ALL output.

- This scan is **required** - do not skip this step
- You **must** read and handle the complete scan output (can pipe to temp file)
- Do not sample or skip any results - process every class found
- Prevents lazy analysis by forcing examination of all classes with `__init__` methods

**Goal**: Find ALL classes that are just boilerplate parameter assignments (high recall).

**Recall/Precision**:

- AST-based detection: ~95% recall, ~40% precision (many plain classes are legitimately complex)
- Grep patterns: ~70% recall, ~30% precision (manual inspection required)

**Approach**:

### Step 2: Automated Discovery (Gather Candidates)

**Tool**: `prompts/scans/scan_dataclass_candidates.py` - AST-based scanner for boilerplate classes

**What it finds**:

- All classes with their `__init__` analysis
- Counts `self.x = y` assignments in `__init__`
- Counts other statements in `__init__` (validation, defaults, etc.)
- Flags presence of `__repr__`, `__eq__`, `__hash__` methods

**Usage**:

```bash
# Run on entire codebase
python prompts/scans/scan_dataclass_candidates.py . > dataclass_scan.json

# Filter for high-priority candidates (5+ params)
cat dataclass_scan.json | jq '.classes | to_entries[] |
  {file: .key, classes: [.value | to_entries[] |
  select(.value.init_self_assignments >= 5)]}'
```

**Output structure**:

- `summary`: Total classes and candidate count
- `classes`: Dict mapping file paths to dict of class names to analysis:
  - `line`: Line number
  - `init_self_assignments`: Count of `self.x = y` in `__init__`
  - `init_other_statements`: Count of other statements in `__init__`
  - `has_repr`, `has_eq`, `has_hash`: Boolean flags

**Tool characteristics**:

- **~100% recall**: Finds all classes with `__init__` methods
- **Low precision**: Classes with 5+ params may still need complex init
- **You filter**: Use output to identify candidates, verify manually

**TODO**: Add detection for `to_dict`/`from_dict` methods (Pydantic indicator)

**Grep patterns** (lower recall, useful supplement):

```bash
# Find classes with many parameters (8+ is suspicious)
rg --type py -A20 "def __init__" | rg -B1 "def __init__.*,.*,.*,.*,.*,.*,.*,"

# Find repetitive self.x = x patterns
rg --type py -A10 "def __init__" | rg "self\.\w+ = \w+$" | sort | uniq -c | sort -rn

# Find classes with manual __repr__, __eq__, __hash__ (dataclass provides these)
rg --type py -B5 "def __repr__" | rg "^class"
rg --type py -B5 "def __eq__" | rg "^class"
rg --type py -B5 "def __hash__" | rg "^class"

# Find manual to_dict/from_dict (Pydantic provides these)
rg --type py "def (to_dict|from_dict|asdict|as_dict)"
```

### Step 3: Verification (Filter False Positives)

For each candidate, manually check:

1. **Is initialization truly trivial?**
   - ✅ YES: Just `self.x = x` → Should be dataclass
   - ❌ NO: Complex logic/setup → Plain class is fine

2. **Does it need validation/serialization?**
   - ✅ YES: JSON/dict conversion, validation → Use Pydantic
   - ❌ NO: Internal data structure → Use dataclass

3. **Is it a value object?**
   - ✅ YES: Should be immutable → `@dataclass(frozen=True)`
   - ❌ NO: Mutable state → Regular dataclass

4. **Is it used as dict key / in sets?**
   - ✅ YES: Needs `__hash__` → Frozen dataclass (or plain with custom `__hash__`)
   - ❌ NO: Doesn't need hashing

**Common false positives to skip**:

- Test helper classes (often have complex setup in setUp/fixtures)
- Classes with lifecycle methods (start/stop/cleanup)
- Classes that set up resources (connections, threads, locks)
- Inheritance hierarchies with complex `__init__` chains

### Step 4: Fix Confirmed Issues

Convert identified classes to dataclass/Pydantic:

1. **Plain class → dataclass**: Use `@dataclass` decorator
2. **Plain class → Pydantic**: Inherit from `BaseModel`
3. **Move complex init to `__post_init__`**: If needed after dataclass conversion
4. **Update tests**: Ensure equality/repr still work
5. **Check for breakage**: Some code may depend on specific `__init__` behavior

## Example Scanner Output

Running `scan_dataclass_candidates.py` produces:

```json
{
  "summary": {
    "total_classes": 45,
    "candidate_classes": 8
  },
  "classes": {
    "src/agent/runtime.py": {
      "LocalAgentRuntime": {
        "line": 32,
        "init_self_assignments": 10,
        "init_other_statements": 2,
        "has_repr": true,
        "has_eq": false,
        "has_hash": false
      }
    },
    "src/agent/session.py": {
      "AgentSession": {
        "line": 45,
        "init_self_assignments": 8,
        "init_other_statements": 0,
        "has_repr": false,
        "has_eq": true,
        "has_hash": false
      }
    }
  }
}
```

**Interpretation**:

- `LocalAgentRuntime`: 10 params, 2 other statements → Review (may need complex init)
- `AgentSession`: 8 params, 0 other statements, has `__eq__` → Strong candidate for dataclass

Then manually review each candidate.

## Validation After Conversion

After converting to dataclass/Pydantic, verify:

```bash
# Type checking still passes
mypy path/to/file.py

# Tests still pass
pytest tests/test_module.py

# Check for unexpected __eq__ behavior changes
# (dataclass uses field-wise equality by default)

# Check for __hash__ changes
# (frozen dataclass is hashable, regular is not)

# Check serialization round-trips
python -c "
from mymodule import MyModel
m = MyModel(field1='test', field2=42)
d = m.model_dump()  # or asdict(m) for dataclass
m2 = MyModel.model_validate(d)  # or MyModel(**d)
assert m == m2
"
```

## Common Issues and Fixes

### Issue 1: Mutable Default Arguments

```python
# BAD: Mutable default in dataclass
@dataclass
class Config:
    handlers: list[Handler] = []  # ❌ Shared across instances!

# GOOD: Use field(default_factory=...)
@dataclass
class Config:
    handlers: list[Handler] = field(default_factory=list)
```

### Issue 2: Private Fields

```python
# Dataclass: Use leading underscore in field name
@dataclass
class Runtime:
    _internal: str = field(default="", repr=False)

# Or exclude from __init__ if set later
@dataclass
class Runtime:
    session: Session | None = field(default=None, init=False)
```

### Issue 3: Inheritance

```python
# Dataclasses compose well with inheritance
@dataclass
class Base:
    id: str

@dataclass
class Derived(Base):
    name: str
    # Gets both id and name in __init__
```

### Issue 4: Converting to Frozen Breaks Code

```python
# If code mutates fields, can't use frozen=True
# Solution: Keep mutable, or refactor to immutable pattern
@dataclass
class MutableConfig:
    value: int

    def increment(self):
        self.value += 1  # OK for mutable dataclass

# Or use immutable + replace
@dataclass(frozen=True)
class ImmutableConfig:
    value: int

    def incremented(self) -> ImmutableConfig:
        return replace(self, value=self.value + 1)
```

## When NOT to Convert

**Don't convert if**:

- Class has complex `__init__` logic (validation, setup, transformations)
- Inheritance hierarchy with complex constructor chains
- Need precise control over field order, defaults, or initialization
- Performance-critical tight loop (dataclass has slight overhead)
- External code depends on specific `__init__` signature
- Class is a mixin or abstract base (may not have state)

**Consider keeping plain class when**:

- Parameters undergo non-trivial transformations
- Need to set up resources (connections, locks, threads)
- Complex validation logic that doesn't fit validators
- Stateful lifecycle (start/stop/cleanup methods)

## References

- [Python dataclasses](https://docs.python.org/3/library/dataclasses.html)
- [Pydantic V2 Docs](https://docs.pydantic.dev/latest/)
- [Real Python: Data Classes](https://realpython.com/python-data-classes/)
- [When to use dataclasses vs Pydantic](https://pydantic-docs.helpmanual.io/usage/models/#dataclasses-vs-basemodel)
