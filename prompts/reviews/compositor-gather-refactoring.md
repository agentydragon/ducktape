# Code Review: Compositor Tool Enumeration Pattern

**File**: `adgn/src/adgn/mcp/compositor/server.py:191-205`

## Current Code (Problematic)

```python
# Phase 2: resolve tool enumeration tasks (per-server failure captured individually)
if tool_tasks:
    order = list(tool_tasks.keys())
    results = await asyncio.gather(*(tool_tasks[n] for n in order), return_exceptions=True)
    for nm, res in zip(order, results, strict=False):
        entry = per_name.get(nm)
        if entry is None:
            continue
        if isinstance(res, BaseException):
            per_name[nm] = FailedServerEntry(error=f"{type(res).__name__}: {res}")
        elif isinstance(entry, RunningServerEntry):
            per_name[nm] = RunningServerEntry(initialize=entry.initialize, tools=res)
        # else: ignore tools result for non-running entries (already failed/initializing)

    return per_name
```

## Why This Is Bad ("reeks")

### 1. **Unnecessary Intermediate Data Structures**
- Creates `order = list(tool_tasks.keys())` just to preserve key order
- Rebuilds generator `(tool_tasks[n] for n in order)` from the keys
- Uses `zip(order, results)` to re-associate keys with results
- **Why bad**: This is exactly what `dict.items()` gives you for free

### 2. **Fragile Manual Synchronization**
- Relies on implicit ordering guarantee: keys list → generator → results → zip
- One mistake in any step breaks the name→result association
- **Why bad**: Easy to introduce bugs, hard to verify correctness

### 3. **Redundant Lookups**
- `entry = per_name.get(nm)` looks up entry that was JUST set in Phase 1
- If `entry is None`, means we created a task for a non-existent mount (impossible state)
- **Why bad**: Defensive check for impossible condition, wastes time

### 4. **Doesn't "Thread Through Target Buckets Automatically"**
- Phase 1: Classify mounts into categories (running/failed/initializing)
- Phase 2: Manually re-classify based on tool enumeration results
- **Why bad**: Should flow naturally from Phase 1 classification, not require re-checking

### 5. **gather() Waits for ALL Tasks Even When Some Failed**
- If server A fails in Phase 1, its task still runs in Phase 2
- `gather()` waits for ALL tasks even though some results are discarded
- **Why bad**: Wastes resources on tasks whose results won't be used

## Proposed Refactoring

### Using TaskGroup (Python 3.11+, BEST)

```python
# Phase 2: resolve tool enumeration in parallel with structured concurrency
async with asyncio.TaskGroup() as tg:
    async def _handle_tools(name: str, task: asyncio.Task):
        try:
            tools = await task
            entry = per_name[name]
            if isinstance(entry, RunningServerEntry):
                per_name[name] = RunningServerEntry(initialize=entry.initialize, tools=tools)
        except Exception as e:
            per_name[name] = FailedServerEntry(error=f"{type(e).__name__}: {e}")

    for name, task in tool_tasks.items():
        tg.create_task(_handle_tools(name, task))

# All tasks complete here (or exception propagated if one failed)
return per_name
```

### Why This Is Better

1. **Maintains Concurrency**: All tasks run in parallel like original
2. **No Intermediate Structures**: Direct iteration over `tool_tasks.items()`
3. **Structured Concurrency**: TaskGroup handles cleanup automatically
4. **No Manual Synchronization**: Name captured in closure, no zip/order tracking
5. **Type-Safe**: Entry is guaranteed `RunningServerEntry` from Phase 1
6. **Automatic Cleanup**: On exception, all tasks cancelled automatically

### Alternative: Fix gather() with Proper Tracking (If TaskGroup Not Available)

If stuck on Python <3.11 and gather() is required:

```python
# Phase 2: resolve tool enumeration in parallel (fixed gather pattern)
tasks_by_name = list(tool_tasks.items())
results = await asyncio.gather(
    *[task for _name, task in tasks_by_name],
    return_exceptions=True
)

for (name, _task), result in zip(tasks_by_name, results, strict=True):
    if isinstance(result, BaseException):
        per_name[name] = FailedServerEntry(error=f"{type(result).__name__}: {result}")
    else:
        entry = per_name[name]
        if isinstance(entry, RunningServerEntry):
            per_name[name] = RunningServerEntry(initialize=entry.initialize, tools=result)

return per_name
```

**Why this fixes the original**:
- Uses `list(tool_tasks.items())` instead of separate `keys()` and generator
- `strict=True` on zip catches length mismatches
- Still wasteful (waits for all tasks), but at least correct

**Still inferior to TaskGroup**: gather waits for ALL tasks even if some failed early.

## Broader Design Issues

### Issue: Phase 1 and Phase 2 Have Overlapping Concerns

Phase 1 creates entries AND schedules tasks:
```python
per_name[name] = RunningServerEntry(initialize=init, tools=[])
tool_tasks[name] = asyncio.create_task(...)
```

Phase 2 updates entries based on task results:
```python
per_name[nm] = RunningServerEntry(initialize=entry.initialize, tools=res)
```

**Problem**: Entry state management split across two phases.

### Better Design: Separate Concerns

```python
async def server_entries(self) -> dict[str, ServerEntry]:
    # Phase 1: Initialize all servers, collect initialization results
    init_results = await self._initialize_all_servers()

    # Phase 2: Enumerate tools for successfully initialized servers
    tool_results = await self._enumerate_tools(init_results)

    # Phase 3: Build final entries from init + tool results
    return self._build_entries(init_results, tool_results)
```

Each phase has a single responsibility:
1. **Initialize**: Get `InitializeResult` from each server
2. **Enumerate**: Get `list[Tool]` from successfully initialized servers
3. **Build**: Construct `ServerEntry` union from results

## Summary

The current pattern "reeks" because it:
- Creates unnecessary intermediate lists/generators to preserve ordering
- Manually zips results back to names (dict items already give this)
- Looks up entries that were just set (redundant)
- Waits for all tasks even when some results are discarded
- Splits entry state management across two tightly coupled phases

**Recommendation**: Refactor to either:
1. Sequential await (simpler, easier to debug)
2. Proper TaskGroup (Python 3.11+, best)
3. Keep gather but fix the zip/order/lookup mess (minimal change)
