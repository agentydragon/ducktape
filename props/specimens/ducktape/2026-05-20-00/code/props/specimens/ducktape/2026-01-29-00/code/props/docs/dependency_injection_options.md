# Dependency Injection Options for adgn/props

## Problem Statement

Current issues:

- **Repeated expensive initialization**: `SnapshotHydrator.from_env()`, `docker.from_env()`, `FilesystemLoader()` created multiple times per CLI invocation
- **Manual threading**: Would need to pass dependencies through every layer (CLI → business logic → helpers)
- **Testing friction**: Hard to mock dependencies when they're created inline
- **No lifecycle management**: Docker client never closes, no cleanup hooks

What we need:

- ✅ Explicit dependencies (function signatures show what they need)
- ✅ Single initialization per CLI invocation
- ✅ Easy testing (mock/override for tests)
- ✅ mypy compatibility (preserve type information)
- ✅ Minimal boilerplate (don't want DI to dominate the code)

## Current Anti-Patterns

```python
# ❌ Anti-pattern: Create expensive objects inline
def snapshot_exec(snapshot: str):
    hydrator = SnapshotHydrator.from_env()  # Expensive: loads manifests
    dclient = docker.from_env()             # Expensive: connects to daemon

    async with hydrator.hydrate(snapshot) as hydrated:
        container = dclient.containers.run(...)

# ❌ Anti-pattern: Called 14+ times across codebase
def grade_validation():
    hydrator = SnapshotHydrator.from_env()  # Loads manifests AGAIN
    # ...

def prompt_optimize():
    hydrator = SnapshotHydrator.from_env()  # Loads manifests AGAIN AGAIN
    # ...
```

## Options

### Option 1: typer-di (Recommended)

**What it is**: Drop-in replacement for Typer with FastAPI-style dependency injection

**Code example**:

```python
# cli/resources.py
from typer_di import TyperDI, Depends
import docker
from ..hydration import SnapshotHydrator

app = TyperDI()

# Dependency functions - called once per CLI invocation, cached
def get_hydrator() -> SnapshotHydrator:
    return SnapshotHydrator.from_env()

def get_docker() -> docker.DockerClient:
    client = docker.from_env()
    import atexit
    atexit.register(client.close)
    return client

# Business logic - dependencies explicit in signature
@app.command()
def snapshot_exec(
    snapshot: str,
    hydrator: SnapshotHydrator = Depends(get_hydrator),
    docker_client: docker.DockerClient = Depends(get_docker),
):
    """Types preserved, mypy happy, IDE autocomplete works."""
    async with hydrator.hydrate(snapshot) as hydrated:
        container = docker_client.containers.run(...)

@app.command()
def grade_validation(
    hydrator: SnapshotHydrator = Depends(get_hydrator),  # Reuses same instance
):
    # get_hydrator() called once, result cached and reused
    pass
```

**Testing**:

```python
from unittest.mock import Mock, patch

def test_snapshot_exec():
    mock_hydrator = Mock(spec=SnapshotHydrator)

    with patch("cli.cmd_snapshot.get_hydrator", return_value=mock_hydrator):
        runner = CliRunner()
        result = runner.invoke(app, ["snapshot-exec", "test/slug"])

        mock_hydrator.hydrate.assert_called_once()
```

**Pros**:

- ✅ Minimal changes: `Typer()` → `TyperDI()`
- ✅ Perfect mypy support (full stubs, types preserved)
- ✅ Dependencies cached per invocation automatically
- ✅ Explicit function signatures (clear what each command needs)
- ✅ No container boilerplate
- ✅ Standard Python testing (patch dependency functions)

**Cons**:

- ⚠️ New dependency (but small: ~500 LOC, no subdeps)
- ⚠️ Testing requires patching (standard Python pattern, but some find verbose)
- ❌ **No async support**: Dependency functions must be synchronous (see "Async Dependencies" section below)

**When to use**: Default choice for Typer CLI apps with synchronous dependencies

---

### Option 2: dependency-injector (Full-Featured)

**What it is**: Industrial-strength DI framework with containers and providers

**Code example**:

```python
# cli/container.py
from dependency_injector import containers, providers
import docker
from ..hydration import SnapshotHydrator

class Container(containers.DeclarativeContainer):
    """Central registry of all dependencies."""

    config = providers.Configuration()

    hydrator = providers.Singleton(SnapshotHydrator.from_env)
    docker_client = providers.Singleton(docker.from_env)
    filesystem_loader = providers.Factory(
        FilesystemLoader,
        base_path=providers.Callable(specimens_definitions_root),
    )

# cli/main.py
from dependency_injector.wiring import Provide, inject

container = Container()
container.wire(modules=["cli.cmd_snapshot", "cli.cmd_grade", ...])

@app.command()
@inject  # Must be innermost decorator
def snapshot_exec(
    snapshot: str,
    hydrator: SnapshotHydrator = Provide[Container.hydrator],
    docker_client: docker.DockerClient = Provide[Container.docker_client],
):
    async with hydrator.hydrate(snapshot) as hydrated:
        container = docker_client.containers.run(...)
```

**Testing**:

```python
def test_snapshot_exec():
    mock_hydrator = Mock(spec=SnapshotHydrator)

    # Clean override API
    with container.hydrator.override(mock_hydrator):
        runner = CliRunner()
        result = runner.invoke(app, ["snapshot-exec", "test/slug"])

        mock_hydrator.hydrate.assert_called_once()
```

**Pros**:

- ✅ Perfect mypy support (full stubs, Cython-optimized)
- ✅ Clean test override API (`container.provider.override(mock)`)
- ✅ Advanced features (configuration, resources, scopes)
- ✅ Battle-tested in production (FastAPI, Flask, Django integration)

**Cons**:

- ❌ More boilerplate (Container class, wiring, `@inject` decorator)
- ❌ Must remember to wire modules
- ❌ `@inject` position matters (must be innermost)
- ❌ Steeper learning curve

**When to use**: Multi-framework applications, need advanced DI features (scopes, resources, configuration management)

---

### Option 3: Protocol-Based Manual DI (No Library)

**What it is**: Explicit dependency passing using Protocols for interface contracts

**Code example**:

```python
# core/resources.py
from typing import Protocol
import docker
from ..hydration import SnapshotHydrator

class HydratorProvider(Protocol):
    def get_hydrator(self) -> SnapshotHydrator: ...

class DockerProvider(Protocol):
    def get_docker(self) -> docker.DockerClient: ...

class Resources:
    """Concrete implementation - lazy initialization."""

    def __init__(self):
        self._hydrator: SnapshotHydrator | None = None
        self._docker: docker.DockerClient | None = None

    def get_hydrator(self) -> SnapshotHydrator:
        if self._hydrator is None:
            self._hydrator = SnapshotHydrator.from_env()
        return self._hydrator

    def get_docker(self) -> docker.DockerClient:
        if self._docker is None:
            self._docker = docker.from_env()
        return self._docker

    def close(self):
        if self._docker is not None:
            self._docker.close()

# cli/commands.py - Business logic, no Typer
def snapshot_exec_impl(
    snapshot: str,
    *,
    hydrator: HydratorProvider,
    docker: DockerProvider,
) -> int:
    """Pure logic - testable without Typer."""
    hydrator_impl = hydrator.get_hydrator()
    docker_client = docker.get_docker()

    async with hydrator_impl.hydrate(snapshot) as hydrated:
        container = docker_client.containers.run(...)
    return 0

# cli/main.py - Thin Typer wrappers
@app.callback()
def setup(ctx: typer.Context):
    resources = Resources()
    ctx.obj = resources
    import atexit
    atexit.register(resources.close)

@app.command()
def snapshot_exec(ctx: typer.Context, snapshot: str):
    """Thin wrapper - just marshals CLI args."""
    exit_code = snapshot_exec_impl(
        snapshot=snapshot,
        hydrator=ctx.obj,
        docker=ctx.obj,
    )
    raise typer.Exit(exit_code)
```

**Testing**:

```python
from unittest.mock import Mock

def test_snapshot_exec():
    """Test pure business logic without CLI."""
    mock_hydrator = Mock(spec=HydratorProvider)
    mock_docker = Mock(spec=DockerProvider)

    exit_code = snapshot_exec_impl(
        snapshot="test/slug",
        hydrator=mock_hydrator,
        docker=mock_docker,
    )

    assert exit_code == 0
    mock_hydrator.get_hydrator.assert_called_once()
```

**Pros**:

- ✅ No library dependency
- ✅ Perfect mypy support (Protocols are first-class)
- ✅ Explicit contracts (Protocol defines interface)
- ✅ Business logic separate from CLI (testable without Typer)
- ✅ Very testable (pass mocks directly)

**Cons**:

- ❌ More boilerplate (Protocol definitions, wrapper functions)
- ❌ Manual context passing (`ctx.obj`)
- ❌ Separation between CLI wrappers and business logic (extra indirection)

**When to use**: Maximum explicitness, no external dependencies, want business logic fully decoupled from CLI

---

### Option 4: Hybrid Approach

**What it is**: Mix strategies based on component needs

**Code example**:

```python
# Shared singletons via module-level memoization
from functools import lru_cache

@lru_cache(maxsize=1)
def get_hydrator() -> SnapshotHydrator:
    return SnapshotHydrator.from_env()

# Per-request resources via typer-di
from typer_di import TyperDI, Depends

def get_session() -> Iterator[Session]:
    """Database session - not cached, new per command."""
    with _get_session() as session:
        yield session

@app.command()
def grade_validation(
    hydrator: SnapshotHydrator = Depends(get_hydrator),  # Cached
    session: Session = Depends(get_session),             # Fresh per command
):
    # hydrator reused across commands
    # session is fresh (not shared)
    pass
```

**Pros**:

- ✅ Flexibility (choose right pattern per use case)
- ✅ Minimal library lock-in
- ✅ Progressive adoption (migrate incrementally)

**Cons**:

- ⚠️ Inconsistency (multiple patterns in same codebase)
- ⚠️ Team needs to know when to use which pattern

**When to use**: Legacy codebase migration, want to evaluate before committing

---

## Recommendation for adgn/props

### Primary: Use typer-di

**Why**:

1. **Minimal disruption**: Change `Typer()` → `TyperDI()`, add `Depends()` to commands
2. **Perfect for CLI**: Designed specifically for Typer, zero impedance mismatch
3. **Good enough DI**: Handles 90% of use cases (singletons, caching, lifecycle)
4. **Type-safe**: Full mypy support, IDE autocomplete works
5. **Simple testing**: Standard Python mocking patterns

**What gets injected** (resources that should be created once):

- `SnapshotHydrator` (expensive: loads all manifests from YAML)
- `docker.DockerClient` (expensive: connects to daemon)
- `FilesystemLoader` (scans filesystem for issue files)
- Config/settings objects (if added later)

**What stays direct** (cheap or need fresh instances):

- `get_session()` - database sessions (must be per-operation)
- `specimens_definitions_root()` - could be injected, but cheap enough to call directly
- Pure functions with no state

### Implementation Plan

#### Phase 1: Setup Foundation (1 hour)

```python
# props/cli/resources.py
"""Shared resources for CLI commands (DI providers)."""

from functools import cache
import atexit
import docker
from ..hydration import SnapshotHydrator
from ..db.sync._sync import FilesystemLoader
from ..runs_context import specimens_definitions_root

# Singleton resources (created once, reused)
@cache
def get_hydrator() -> SnapshotHydrator:
    """Get snapshot hydrator (loads manifests once)."""
    return SnapshotHydrator.from_env()

@cache
def get_docker_client() -> docker.DockerClient:
    """Get Docker client (connects once, cleaned up on exit)."""
    client = docker.from_env()
    atexit.register(client.close)
    return client

def get_filesystem_loader() -> FilesystemLoader:
    """Get filesystem loader for specimens."""
    return FilesystemLoader(specimens_definitions_root())
```

```python
# props/cli/main.py
from typer_di import TyperDI, Depends  # Change import

# Change this:
# app = Typer()
# To this:
app = TyperDI()
```

#### Phase 2: Migrate Commands (incremental, command by command)

**Before**:

```python
@app.command()
def snapshot_exec(snapshot: str, command: list[str] | None = None):
    hydrator = SnapshotHydrator.from_env()  # ❌ Expensive
    dclient = docker.from_env()             # ❌ Expensive

    async with hydrator.hydrate(snapshot) as hydrated:
        ...
```

**After**:

```python
from .resources import get_hydrator, get_docker_client
from typer_di import Depends

@app.command()
def snapshot_exec(
    snapshot: str,
    command: list[str] | None = None,
    hydrator: SnapshotHydrator = Depends(get_hydrator),
    docker_client: docker.DockerClient = Depends(get_docker_client),
):
    # ✅ hydrator and docker_client injected, reused across commands
    async with hydrator.hydrate(snapshot) as hydrated:
        ...
```

**Migration order** (prioritize high-use commands):

1. `snapshot exec` (uses both hydrator and Docker)
2. `grade-validation` (uses hydrator)
3. `prompt-optimize` (uses hydrator)
4. Remaining commands as needed

#### Phase 3: Update Tests

**Before**:

```python
def test_snapshot_exec():
    # ❌ Hits real filesystem, Docker daemon
    result = runner.invoke(app, ["snapshot-exec", "test/slug"])
```

**After**:

```python
from unittest.mock import Mock, patch

def test_snapshot_exec():
    mock_hydrator = Mock(spec=SnapshotHydrator)
    mock_docker = Mock(spec=docker.DockerClient)

    with patch("cli.resources.get_hydrator", return_value=mock_hydrator):
        with patch("cli.resources.get_docker_client", return_value=mock_docker):
            result = runner.invoke(app, ["snapshot-exec", "test/slug"])

            assert result.exit_code == 0
            mock_hydrator.hydrate.assert_called_once()
```

**Or use pytest fixtures**:

```python
@pytest.fixture
def mock_resources(monkeypatch):
    """Fixture that mocks all CLI resources."""
    mock_hydrator = Mock(spec=SnapshotHydrator)
    mock_docker = Mock(spec=docker.DockerClient)

    monkeypatch.setattr("cli.resources.get_hydrator", lambda: mock_hydrator)
    monkeypatch.setattr("cli.resources.get_docker_client", lambda: mock_docker)

    return {
        "hydrator": mock_hydrator,
        "docker": mock_docker,
    }

def test_snapshot_exec(mock_resources):
    result = runner.invoke(app, ["snapshot-exec", "test/slug"])

    assert result.exit_code == 0
    mock_resources["hydrator"].hydrate.assert_called_once()
```

---

## Comparison Matrix

| Feature             | typer-di        | dependency-injector | Protocol-based | Hybrid   |
| ------------------- | --------------- | ------------------- | -------------- | -------- |
| **Boilerplate**     | Minimal         | Medium-High         | Medium         | Variable |
| **mypy Support**    | ✅ Excellent    | ✅ Excellent        | ✅ Perfect     | ✅ Good  |
| **Learning Curve**  | Gentle          | Steep               | Moderate       | Variable |
| **Testing**         | Patch functions | Override providers  | Pass mocks     | Mixed    |
| **CLI Integration** | ✅ Native       | ⚠️ Manual           | ⚠️ Wrappers    | Variable |
| **Type Safety**     | ✅ Full         | ✅ Full             | ✅ Full        | ✅ Good  |
| **External Deps**   | 1 (typer-di)    | 1 (dep-injector)    | 0              | 0-1      |
| **Maintenance**     | Low             | Medium              | Low            | Medium   |

---

## FAQ

### Q: Why not just use module-level singletons?

```python
# ❌ Anti-pattern: Module-level singleton
_hydrator: SnapshotHydrator | None = None

def get_hydrator() -> SnapshotHydrator:
    global _hydrator
    if _hydrator is None:
        _hydrator = SnapshotHydrator.from_env()
    return _hydrator
```

**Problems**:

- Global state (testing requires cleanup)
- Manual lifecycle management (no cleanup hooks)
- Not composable (can't pass different instances)
- Hidden dependencies (function signature doesn't show needs)

**typer-di solves this**:

- No globals (dependencies scoped to CLI invocation)
- Automatic lifecycle (cleanup via atexit)
- Testable (patch the function)
- Explicit (function signature shows `= Depends(get_hydrator)`)

### Q: What about `get_session()`? Should it be injected?

**No**. Database sessions should be:

- Short-lived (per-operation, not per-CLI-invocation)
- Context-managed (auto-commit/rollback)
- Not cached (fresh connection per operation)

Keep the current pattern:

```python
def some_command(hydrator: SnapshotHydrator = Depends(get_hydrator)):
    # hydrator is singleton (reused)

    with get_session() as session:  # Fresh session per operation
        snapshot = session.query(Snapshot).filter_by(...).one()
```

### Q: How do I inject into helpers/business logic?

**Option A: Thread through parameters** (explicit):

```python
def grade_validation(
    critique_id: int,
    hydrator: SnapshotHydrator = Depends(get_hydrator),
):
    # Call helper with explicit params
    result = _grade_impl(critique_id, hydrator)

def _grade_impl(critique_id: int, hydrator: SnapshotHydrator) -> dict:
    """Business logic - dependencies explicit in signature."""
    async with hydrator.hydrate(...) as hydrated:
        ...
```

**Option B: Make helpers dependency functions** (if they're reusable):

```python
def get_grader(hydrator: SnapshotHydrator = Depends(get_hydrator)) -> Grader:
    """Factory for grader (depends on hydrator)."""
    return Grader(hydrator)

@app.command()
def grade_validation(
    critique_id: int,
    grader: Grader = Depends(get_grader),  # Nested dependency
):
    result = grader.grade(critique_id)
```

### Q: Can I mix typer-di with plain Typer?

**Yes**. `TyperDI` is backwards-compatible:

```python
app = TyperDI()

@app.command()
def old_command(name: str):
    """No dependencies - works fine."""
    print(name)

@app.command()
def new_command(
    name: str,
    hydrator: SnapshotHydrator = Depends(get_hydrator),
):
    """Uses DI - also works fine."""
    async with hydrator.hydrate(name) as hydrated:
        ...
```

Migrate incrementally. No flag day required.

---

## Alternative Considered: Don't DI, just optimize

**Could we just optimize the current pattern instead?**

```python
# Option: Module-level memoization
from functools import lru_cache

@lru_cache(maxsize=1)
def _get_hydrator_cached() -> SnapshotHydrator:
    return SnapshotHydrator.from_env()

@app.command()
def snapshot_exec(snapshot: str):
    hydrator = _get_hydrator_cached()  # Cached, but still hidden
    ...
```

**Why this is worse**:

- ❌ Hidden dependencies (signature doesn't show needs)
- ❌ Hard to test (need to clear cache, patch hidden function)
- ❌ No lifecycle management (can't register cleanup)
- ❌ Not composable (all commands get same instance, can't override)

**DI is better**:

- ✅ Explicit dependencies (signature shows `= Depends(...)`)
- ✅ Testable (patch the dependency function)
- ✅ Lifecycle hooks (atexit cleanup)
- ✅ Composable (can override per-command if needed)

---

## Final Recommendation

**Use typer-di** for adgn/props CLI:

1. **Add dependency**: `pip install typer-di` (or add to pyproject.toml)
2. **Create `cli/resources.py`**: Define dependency functions
3. **Change `Typer()` to `TyperDI()`**: One-line change in main.py
4. **Migrate commands incrementally**: Add `Depends()` to high-use commands first
5. **Update tests**: Use `patch()` or fixtures to mock dependencies

**Timeline**:

- Setup: 30 minutes
- Migrate 1-2 commands: 1 hour
- Update tests: 1 hour
- Complete migration: Incremental (as needed)

**Total effort**: ~3 hours for core migration, then incremental refinement.

**Benefits**:

- 🚀 Performance: No repeated expensive initialization
- 🧪 Testability: Easy to mock dependencies
- 📝 Clarity: Function signatures show dependencies
- 🔧 Maintainability: Centralized resource management

---

## Async Dependencies (typer-di Limitation)

### The Problem

**typer-di does not support async dependency functions.** This is a fundamental limitation because:

1. **Dependency injection happens synchronously**: typer-di generates wrapper code that calls dependency functions directly (not with `await`)
2. **Calls happen before event loop starts**: Dependencies are resolved during parameter binding, before `@async_run` starts the event loop
3. **Underlying Typer has no async support**: Typer itself [doesn't natively support async functions](https://github.com/fastapi/typer/issues/88)

**Source code evidence** (from `/code/typer_di/src/typer_di/_method_builder.py`):

```python
# Line 48-50: Generated code template
_INVOKE_TEMPLATE = """\
    {result} = {callback}({args})   # ❌ No 'await' - synchronous only
"""
```

The generated wrapper looks like:

```python
def wrapper(param1, param2):
    __r0 = dependency_fn1(arg1=param1)  # Called synchronously
    __r1 = dependency_fn2(arg2=__r0)    # Called synchronously
    return __r1
```

### Impact on `aiodocker.Docker`

**Newer versions of `aiodocker` require a running event loop** in the constructor:

```python
# aiodocker/docker.py:138
connector = aiohttp.UnixConnector(docker_host[...])
# ❌ Internally calls asyncio.get_running_loop() - fails if no loop
```

This breaks the previous pattern where `get_async_docker_client()` returned `aiodocker.Docker()`:

```python
# ❌ BROKEN: No event loop when typer-di calls this
def get_async_docker_client() -> aiodocker.Docker:
    return aiodocker.Docker()  # RuntimeError: no running event loop

@app.command()
@async_run
async def my_command(
    docker: aiodocker.Docker = Depends(get_async_docker_client),  # ❌ Fails
):
    ...
```

### Solution: Create Async Resources Inside Commands

**Don't use `Depends()` for async resources.** Create them directly in the async command body:

```python
# ✅ CORRECT: Create Docker client inside event loop
@app.command()
@async_run
async def my_command(
    snapshot: str,
    hydrator: SnapshotHydrator = Depends(get_hydrator),  # ✅ Sync dependency - OK
):
    # Create async resources here (inside event loop)
    docker_client = aiodocker.Docker()
    try:
        # Use docker_client
        async with docker_client.containers.run(...) as container:
            ...
    finally:
        await docker_client.close()  # Important: cleanup
```

**Pattern for shared async resources**:

If you need to reuse an async resource across multiple operations in the same command:

```python
async def _with_docker_client(
    operation: Callable[[aiodocker.Docker], Awaitable[T]]
) -> T:
    """Helper to manage Docker client lifecycle."""
    docker = aiodocker.Docker()
    try:
        return await operation(docker)
    finally:
        await docker.close()

@app.command()
@async_run
async def my_command(snapshot: str):
    async def _run_container(docker: aiodocker.Docker):
        container = await docker.containers.run(...)
        # ... work with container
        return result

    result = await _with_docker_client(_run_container)
```

### Alternative: Lazy Async Factory Pattern

If you really need dependency injection for async resources, use a factory pattern:

```python
# resources.py
from collections.abc import Callable, Awaitable

def get_docker_factory() -> Callable[[], Awaitable[aiodocker.Docker]]:
    """Return a factory that creates Docker clients (not a client itself)."""
    return aiodocker.Docker  # Return the class, don't call it

# Command
@app.command()
@async_run
async def my_command(
    docker_factory: Callable[[], Awaitable[aiodocker.Docker]] = Depends(get_docker_factory),
):
    docker = await docker_factory()  # Now we're in event loop
    try:
        ...
    finally:
        await docker.close()
```

**But this is more complex than just creating the client inline.** Prefer the direct creation pattern.

### What Works with typer-di

✅ **Synchronous dependencies**:

- `SnapshotHydrator.from_env()` - loads YAML synchronously
- `docker.from_env()` - sync Docker client (not `aiodocker`)
- Database connections (if using sync SQLAlchemy)
- Config loading
- File system operations

❌ **Async dependencies** (create inline instead):

- `aiodocker.Docker()` - requires event loop
- `aiohttp.ClientSession()` - requires event loop
- Async database pools
- Anything using `await` in initialization

### Related Links

- [Typer Issue #88: How to use with async?](https://github.com/fastapi/typer/issues/88)
- [Typer Issue #950: Async support](https://github.com/fastapi/typer/issues/950)
- [typer-di source code](https://github.com/greendwin/typer_di)
