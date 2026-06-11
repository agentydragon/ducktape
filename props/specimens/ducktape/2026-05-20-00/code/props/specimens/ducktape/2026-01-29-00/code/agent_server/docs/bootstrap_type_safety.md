# Bootstrap Type Safety: Design Plans

## Refactoring Status (December 2025)

**✅ Core Refactoring Complete**

All bootstrap handlers have been migrated to the new pattern:

- ✅ `LinterController` → `make_linter_bootstrap_calls()` + `BootstrapHandler` + `GateUntil`
- ✅ `CommitController` → Uses `TypedBootstrapBuilder` (removed `ItemFactory`)
- ✅ `BootstrapInspectHandler` → Uses `TypedBootstrapBuilder` and helper functions
- ✅ Zero `ItemFactory` usage remaining for bootstrap
- ✅ Zero global module-level bootstrap state
- ✅ Integration tests added (`tests/agent/test_bootstrap_integration.py`)
- ✅ Documentation updated (`AGENTS.md`)

**Files refactored:**

- `agent_core/bootstrap.py` - Core infrastructure
- `props/core/lint_issue.py` - LinterController split
- `adgn/gitea_pr_gate/minicodex_backend.py` - CommitController refactored
- `props/core/lint_issue.py` - BootstrapInspectHandler updated

## Current State (Post-Refactoring)

After the recent refactoring, bootstrap calls are constructed using `TypedBootstrapBuilder`:

```python
# Current pattern
builder = TypedBootstrapBuilder.for_server(runtime_server)
calls = [
    builder.call("runtime", "docker_exec", ExecInput(cmd=["ls", "-la"])),
    builder.call("git_ro", "git_status", StatusInput(...)),
]
bootstrap = BootstrapHandler(calls)
```

**Issues with current approach:**

- ❌ String literals for server names: `"runtime"`, `"git_ro"`
- ❌ String literals for tool names: `"docker_exec"`, `"git_status"`
- ❌ Typos only caught at runtime
- ❌ No IDE autocomplete for tool names
- ❌ Not refactor-safe (renaming doesn't update string literals)

## Goal: Eliminate String Literals

**Desired syntax:**

```python
# Option A: Direct attribute access
bootstrap.runtime.docker_exec(ExecInput(cmd=["ls", "-la"]))

# Option B: Via stubs
runtime_stub.docker_exec(ExecInput(cmd=["ls", "-la"]))
```

**Key requirements:**

- ✅ No string literals for server or tool names
- ✅ Type-safe: Pydantic models validated at construction
- ✅ IDE autocomplete support
- ✅ Refactor-safe (following references)
- ⚠️ Acceptable meta-programming complexity

## Plan A: Generic Bootstrap Stub (Recommended Phase 1)

### Design

```python
class GenericBootstrapStub:
    """Generic stub for creating bootstrap calls via attribute access.

    Usage:
        builder = TypedBootstrapBuilder.for_server(runtime_server)
        runtime = GenericBootstrapStub(builder, "runtime")

        # Type-safe: ExecInput validated against runtime server's docker_exec tool
        call = runtime.docker_exec(ExecInput(cmd=["ls", "-la"]))
    """

    def __init__(self, builder: TypedBootstrapBuilder, server_name: str):
        self._builder = builder
        self._server_name = server_name

    def __getattr__(self, tool_name: str) -> Callable[[BaseModel], FunctionCallItem]:
        """Return a callable that creates a bootstrap call for the named tool."""
        def _call(payload: BaseModel) -> FunctionCallItem:
            return self._builder.call(self._server_name, tool_name, payload)
        return _call
```

### Usage Example

```python
# Setup
comp = Compositor("compositor")
runtime_server = await attach_runtime(comp)
git_server = await attach_git_ro(comp, repo_root)

builder = TypedBootstrapBuilder.for_server(runtime_server)

# Create stubs
runtime = GenericBootstrapStub(builder, "runtime")
git = GenericBootstrapStub(builder, GIT_RO_SERVER_NAME)

# Construct bootstrap calls
calls = [
    # No string literals! Tool names via attribute access
    runtime.docker_exec(ExecInput(cmd=["ls", "-la"])),
    git.git_status(StatusInput(list_slice=ListSlice(offset=0, limit=1000))),
    git.git_diff(DiffInput(format=DiffFormat.PATCH, staged=True, ...)),
]

bootstrap = BootstrapHandler(calls)
```

### Benefits

✅ **Eliminates tool name string literals**: `runtime.docker_exec` instead of `"docker_exec"`
✅ **Minimal meta-programming**: Just `__getattr__` (well-understood pattern)
✅ **Type-safe payloads**: Pydantic models validated via `builder.call()`
✅ **Simple implementation**: ~15 lines of code
✅ **No registration overhead**: Create stubs on-demand

### Limitations

⚠️ **Still has server name literal**: Need `"runtime"` string when creating stub
⚠️ **No IDE autocomplete for tools**: `__getattr__` returns generic `Callable`
⚠️ **Typos caught at runtime**: Misspelled tool names fail when call is made
⚠️ **No compile-time verification**: Can't statically verify tool exists on server

## Plan B: Typed Bootstrap Stub (Optional Phase 2)

### Design

```python
class BootstrapStub:
    """Base class for typed bootstrap stubs with explicit method definitions.

    Subclasses define tool methods with type hints that get auto-wired to
    builder.call() at runtime.

    Example:
        class RuntimeBootstrap(BootstrapStub):
            def docker_exec(self, input: ExecInput) -> FunctionCallItem:
                raise NotImplementedError  # Auto-wired at runtime

            def docker_list(self, input: ListInput) -> FunctionCallItem:
                raise NotImplementedError  # Auto-wired at runtime

        # Usage
        builder = TypedBootstrapBuilder.for_server(runtime_server)
        runtime = RuntimeBootstrap.from_builder(builder, "runtime")
        call = runtime.docker_exec(ExecInput(cmd=["ls"]))
    """

    def __init__(self, builder: TypedBootstrapBuilder, server_name: str):
        self._builder = builder
        self._server_name = server_name
        self._auto_wire_methods()

    def _auto_wire_methods(self) -> None:
        """Auto-wire methods based on type hints and return type."""
        for name, method in inspect.getmembers(self.__class__, predicate=inspect.isfunction):
            # Skip special methods and inherited from BootstrapStub
            if name.startswith("_") or hasattr(BootstrapStub, name):
                continue

            # Get type hints
            hints = get_type_hints(method)

            # Verify return type is FunctionCallItem
            if hints.get("return") != FunctionCallItem:
                continue

            # Create wrapper that extracts first param and calls builder
            def _wrapper(self, payload: BaseModel, _tool_name=name) -> FunctionCallItem:
                return self._builder.call(self._server_name, _tool_name, payload)

            # Bind wrapper to instance
            setattr(self, name, _wrapper.__get__(self, self.__class__))

    @classmethod
    def from_builder(
        cls: type[TStub],
        builder: TypedBootstrapBuilder,
        server_name: str
    ) -> TStub:
        return cast(TStub, cls(builder, server_name))
```

### Benefits Over Plan A

✅ **Full IDE autocomplete**: Explicit method definitions visible to IDE
✅ **Static type checking**: Tool existence verified at stub definition
✅ **Self-documenting**: Interface shows all available tools
✅ **Compile-time safety**: Typos in tool names caught immediately
✅ **Better refactoring**: Renaming methods updates all references

### Additional Limitations

❌ **Requires stub definitions**: Manual work to create stub classes
❌ **Maintenance burden**: Stubs must be kept in sync with servers
❌ **More code**: Each server needs a stub class
❌ **Complex meta-programming**: Auto-wiring via introspection

## Comparison Matrix

| Feature                       | Current | Plan A (Generic)       | Plan B (Typed)         |
| ----------------------------- | ------- | ---------------------- | ---------------------- |
| **Server name literals**      | ❌ Yes  | ⚠️ Yes (stub creation) | ⚠️ Yes (stub creation) |
| **Tool name literals**        | ❌ Yes  | ✅ No                  | ✅ No                  |
| **IDE autocomplete (tools)**  | ❌ No   | ❌ No                  | ✅ Yes                 |
| **Type safety (payloads)**    | ✅ Yes  | ✅ Yes                 | ✅ Yes                 |
| **Implementation complexity** | Simple  | Simple                 | Moderate               |
| **Maintenance burden**        | Low     | Low                    | High                   |
| **Refactor safety**           | Low     | Moderate               | High                   |

## Recommendation

### Immediate (Phase 1): Implement Plan A (Generic Stubs)

**Rationale:**

- Simple implementation (~15 LOC)
- Eliminates tool name string literals
- No maintenance burden
- Good enough for most use cases

**Example usage pattern:**

```python
builder = TypedBootstrapBuilder.for_server(runtime_server)
runtime = GenericBootstrapStub(builder, "runtime")

calls = [
    runtime.docker_exec(ExecInput(cmd=["ls", "-la"])),
    # vs current: builder.call("runtime", "docker_exec", ExecInput(...))
]
```

### Future (Phase 2): Add Plan B for High-Value Servers

**When to use:**

- Servers with many tools (e.g., `runtime`, `git_ro`)
- Frequently used in bootstrap code
- Want IDE autocomplete and static checking

**Implementation strategy:**

- Keep `GenericBootstrapStub` as default
- Add `BootstrapStub` base class for opt-in typed stubs
- Create stubs only for commonly used servers
- Document when each approach is appropriate

## Server Name Literal Problem

All plans still require server name string literals when creating stubs. This is acceptable because:

1. **Server names are configuration**: They're defined once at mount time
2. **Constrained scope**: Server names appear in one place (stub creation)
3. **Tool names are the problem**: They appear many times in bootstrap call construction
4. **Diminishing returns**: Eliminating server name literals requires extensive registry infrastructure

## Implementation Roadmap

### Phase 1: Generic Stubs (Week 1)

1. Add `GenericBootstrapStub` to `bootstrap.py`
2. Update helper functions to accept stubs
3. Add usage examples to docstrings
4. Write tests demonstrating the pattern
5. Update 2-3 existing usages as examples

### Phase 2: Typed Stubs (Future)

1. Add `BootstrapStub` base class
2. Create `RuntimeBootstrap` and `GitBootstrap` stubs
3. Document when to use typed vs generic
4. Add auto-wiring tests
5. Consider code generation tool

## Other Followups Not Covered Above

### Helper Functions (Low Priority)

**Status**: Optional, add as patterns emerge
**Location**: `agent_core/bootstrap.py:238` (TODO comment)
**Current helpers**: `builder.read_resource()` (method), `docker_exec_call()` (standalone function)
**Recommendation**: Add more (e.g., `git_diff_call()`, `git_status_call()`) only when repetition justifies it. Scope appropriately (per-module or conftest, not global). Consider moving standalone helpers to methods on `TypedBootstrapBuilder` for consistency.

### Verify BootstrapInspectHandler Usage

**Status**: Low priority verification
**Usage**: Imported by `critic.py` and `grader.py` (via `lint_issue.py`)
**Note**: Handler has been refactored to use new pattern, but verify behavior in critic/grader flows if issues arise.

### Future Test Coverage

**Current**: Integration tests cover core bootstrap flow
**Optional**: Add specific tests for:

- `BootstrapInspectHandler` with different file scopes
- CommitController with `amend=True` mode
- GateUntil + BootstrapHandler coordination patterns

## References

- **Existing patterns**: `mcp_infra/stubs/server_stubs.py` (ServerStub base class)
- **Similar approach**: `mcp_infra/stubs/typed_stubs.py` (TypedClient with **getattr**)
- **Bootstrap refactoring**: Recent work eliminating inheritance, adding TypedBootstrapBuilder
- **MCP tool naming**: `mcp_infra/naming.py` (build_mcp_function convention)
- **Integration tests**: `tests/agent/test_bootstrap_integration.py` (6 test cases)
- **Documentation**: `AGENTS.md` (Bootstrap Handlers section)
