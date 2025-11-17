# Nullability Fixes Proposal

Analysis of nullability issues from the "Optional at ONE branch point, not infecting 500 inner points" perspective.

---

## Issue 1: JSON Helpers - Parameters That Immediately Assert Not None

**Location**: `adgn/src/adgn/mcp/_shared/json_helpers.py:38,65`

### Current Code (BAD - None Infection)

```python
def read_line_json_dict(inp: IO[bytes] | None, timeout: float | None = None) -> dict[str, Any] | None:
    """Sync read a line of JSON from a stream and return as dict or None."""
    assert inp is not None  # ❌ Why accept None if we immediately fail?
    line = inp.readline()
    ...

def send_line_json(out: IO[bytes] | None, payload: dict[str, Any]) -> None:
    """Sync send a JSON payload as a line to a stream."""
    assert out is not None  # ❌ Why accept None if we immediately fail?
    line = json.dumps(payload).encode() + b"\n"
    ...
```

### Problem

- Parameters typed as `IO[bytes] | None` but immediately assert not None
- This is defensive programming against bad calls that mypy should catch
- Callers never pass None (all pass `p.stdout` or similar subprocess streams)
- The `| None` serves no purpose except to propagate None-checking burden

### Proposed Fix (GOOD - Non-Nullable Parameters)

```python
def read_line_json_dict(inp: IO[bytes], timeout: float | None = None) -> dict[str, Any] | None:
    """Sync read a line of JSON from a stream and return as dict or None."""
    # No assertion needed - inp is guaranteed non-None by type system
    line = inp.readline()
    ...

def send_line_json(out: IO[bytes], payload: dict[str, Any]) -> None:
    """Sync send a JSON payload as a line to a stream."""
    # No assertion needed - out is guaranteed non-None by type system
    line = json.dumps(payload).encode() + b"\n"
    ...
```

**Impact**:
- Simpler function signatures
- Let mypy catch bad calls at compile time instead of runtime assertion
- Zero change to call sites (they already pass non-None values)

**Verification**: Check all call sites to confirm they never pass None

```bash
rg "read_line_json_dict\(" --type py
rg "send_line_json\(" --type py
```

All callers pass `p.stdout`, `p.stdin` - never None.

---

## Issue 2: Container Fields - Optional at Construction But Required After Start

**Location**: `adgn/src/adgn/agent/runtime/container.py`

### Current Code (BAD - None Infection Through Container Lifecycle)

```python
@dataclass
class Container:
    approval_engine: ApprovalPolicyEngine | None = None  # ❌ Optional everywhere
    _compositor: Compositor | None = None                # ❌ Optional everywhere

    async def Start(...):
        # Initialize these fields
        self.approval_engine = make_policy_engine(...)
        self._compositor = Compositor(...)

        await self._attach_inproc_servers(ui_bus)

    async def _attach_inproc_servers(self, ui_bus: ServerBus | None) -> None:
        engine = self.approval_engine
        assert engine is not None  # ❌ None infection here

        assert self._compositor is not None  # ❌ And here

        # Use engine and compositor 500 times below
        reader_server = ApprovalPolicyServer(engine, ...)  # mypy error if we remove assert!
        await self._compositor.mount_inproc(...)
        # ... 500 more uses
```

### Problem: None Infection Through Lifecycle Phases

The container has **two distinct lifecycle phases**:
1. **Before Start()**: Fields are None (construction phase)
2. **After Start()**: Fields are guaranteed non-None (operational phase)

But the type system says `| None` **everywhere**, infecting all methods that run during operational phase.

This is the "infecting 500 inner points" antipattern - every method that uses `approval_engine` must handle None, even though it's only None during construction.

### Proposed Fix (GOOD - Optional at ONE Branch Point)

**Strategy**: Separate lifecycle phases using different types or explicit None handling at the boundary.

#### Option A: Separate Construction from Operation

```python
@dataclass
class ContainerConfig:
    """Configuration for container - no None because it's complete."""
    agent_id: str
    persistence: PersistenceInterface
    mcp_config: dict[str, Any]
    with_ui: bool

class Container:
    """Operational container - approval_engine and compositor are ALWAYS present."""

    approval_engine: ApprovalPolicyEngine  # ✅ Not None!
    _compositor: Compositor                # ✅ Not None!

    # Other fields that are truly optional throughout lifecycle
    session: Session | None = None

    @classmethod
    async def Create(cls, config: ContainerConfig, ui_bus: ServerBus | None = None) -> "Container":
        """Factory method - handle None at THIS branch point only."""

        # Initialize non-None fields before constructing Container
        approval_engine = make_policy_engine(
            agent_id=config.agent_id,
            persistence=config.persistence,
        )
        compositor = Compositor(...)

        # Construct with all required fields non-None
        container = cls(
            approval_engine=approval_engine,
            _compositor=compositor,
            agent_id=config.agent_id,
            ...
        )

        await container._attach_inproc_servers(ui_bus)
        return container

    async def _attach_inproc_servers(self, ui_bus: ServerBus | None) -> None:
        # ✅ No assertions needed!
        # approval_engine and _compositor are ALWAYS non-None
        engine = self.approval_engine  # Type: ApprovalPolicyEngine

        # 500 downstream uses - zero None checks
        reader_server = ApprovalPolicyServer(engine, ...)
        await self._compositor.mount_inproc(...)
        proposer_server = ApprovalPolicyProposerServer(engine=engine, ...)
        await self._compositor.mount_inproc(...)
        # ... 500 more uses with NO None checks
```

**Key insight**: Handle None at the **factory method boundary** (Create), then the Container instance always has non-None fields.

#### Option B: Type Narrowing Helper (If Option A is too invasive)

```python
@dataclass
class Container:
    approval_engine: ApprovalPolicyEngine | None = None
    _compositor: Compositor | None = None

    def _require_started(self) -> tuple[ApprovalPolicyEngine, Compositor]:
        """Type-narrowing helper: get non-None engine and compositor after Start().

        Returns:
            Tuple of (engine, compositor), both guaranteed non-None

        Raises:
            RuntimeError: If called before Start() completed
        """
        if self.approval_engine is None or self._compositor is None:
            raise RuntimeError("Container not started - call Start() first")
        return self.approval_engine, self._compositor

    async def _attach_inproc_servers(self, ui_bus: ServerBus | None) -> None:
        # Handle None at THIS branch point only
        engine, compositor = self._require_started()

        # 500 downstream uses with non-None types
        reader_server = ApprovalPolicyServer(engine, ...)
        await compositor.mount_inproc(...)
        # ... 500 more uses with NO None checks
```

**Comparison**:
- **Option A**: More architectural work, but cleaner separation of concerns
- **Option B**: Less invasive, still eliminates None infection in operational methods

Both follow "optional at ONE branch point" - the difference is where that branch point is (factory method vs type-narrowing helper).

---

## Issue 3: Registry - Assert After Create Should Never Fail

**Location**: `adgn/src/adgn/agent/runtime/registry.py`

### Current Code

```python
async def create(...):
    # Create container
    ...

async def restart(...):
    await self.create(agent_id, row.mcp_config, with_ui=with_ui)
    c2 = self.get(agent_id)
    assert c2 is not None  # ❌ This should never fail - we just created it!
    return c2
```

### Problem

- We just created the container with `create(agent_id, ...)`
- Then we immediately assert that `get(agent_id)` returns non-None
- If `get()` can return None after successful `create()`, that's a design bug
- The assertion is defending against our own implementation bug

### Proposed Fix

#### Option 1: Make create() return the container

```python
async def create(...) -> Container:
    """Create and return the container."""
    # Create container
    container = ...
    self._containers[agent_id] = container
    return container

async def restart(...) -> Container:
    # ✅ create() returns non-None, no assertion needed
    return await self.create(agent_id, row.mcp_config, with_ui=with_ui)
```

#### Option 2: Type narrowing in restart()

```python
async def restart(...) -> Container:
    await self.create(agent_id, row.mcp_config, with_ui=with_ui)
    c2 = self.get(agent_id)
    if c2 is None:
        # This should never happen - internal invariant violated
        raise RuntimeError(f"Created container {agent_id} but get() returned None - internal bug")
    return c2
```

**Prefer Option 1** - cleaner API, no defensive programming needed.

---

## Summary: Architectural Patterns

### Pattern 1: Parameters That Immediately Assert → Remove | None

**Before**: `def foo(x: T | None) -> ...: assert x is not None`
**After**: `def foo(x: T) -> ...`

**Benefit**: Let mypy catch bad calls, no runtime assertions needed.

### Pattern 2: Fields Optional During Construction Only → Separate Lifecycle

**Before**: Field is `T | None`, assert not None in 500 operational methods
**After**:
- **Option A**: Factory method constructs with non-None fields
- **Option B**: Type-narrowing helper called ONCE at boundary

**Benefit**: Handle None at ONE branch point (factory/helper), then 500 downstream methods work with non-None.

### Pattern 3: Assert After Create → Return Created Object

**Before**: `create(...); x = get(...); assert x is not None`
**After**: `x = create(...)`  # Returns the created object

**Benefit**: Simpler API, no defensive assertions.

---

## Prioritization

1. **High Priority**: Issue 1 (JSON helpers) - straightforward, no mypy issues, zero risk
2. **Medium Priority**: Issue 3 (Registry) - simple API improvement
3. **Lower Priority**: Issue 2 (Container) - requires more architectural work, but highest impact (eliminates assertions in 500 places)

---

## Implementation Plan

### Phase 1: Low-Hanging Fruit (Issues 1 & 3)

1. Fix `read_line_json_dict` and `send_line_json` signatures
2. Verify mypy passes
3. Fix `Registry.create()` to return Container
4. Update `restart()` to use returned container

**Estimated effort**: 30 minutes
**Risk**: Very low - straightforward signature changes

### Phase 2: Container Lifecycle (Issue 2)

1. Choose between Option A (factory method) or Option B (type-narrowing helper)
2. Implement chosen approach
3. Remove assertions from operational methods
4. Verify mypy passes

**Estimated effort**: 2-3 hours
**Risk**: Medium - touches core Container initialization flow

---

## Verification Strategy

For each fix:

1. **Before**: Count assertions with `rg "assert.*is not None"`
2. **After**: Re-run count - should decrease
3. **Mypy**: Run `mypy --strict` on affected files
4. **Tests**: Run existing tests to verify behavior unchanged

Expected metrics:
- **Assertions removed**: ~10+ (JSON helpers: 2, Container: 5-7, Registry: 1)
- **Lines of None-checking code removed**: ~30-40
- **Functions simplified**: ~15+
