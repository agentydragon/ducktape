# Scan: Asyncio Antipatterns

## Context

@../shared-context.md

## Common Antipatterns

### 0. asyncio.gather() vs TaskGroup: When to Use Each

**Critical principle**: Choose based on whether you need return values and error handling strategy.

#### Use gather() when

1. **You need return values from tasks** (most common case)
2. **Best-effort execution**: Use `return_exceptions=True` to collect all results even if some fail
3. **Result order matters**: gather preserves order of inputs
4. **Python 3.10 or older**: TaskGroup not available

**Example - gather is the RIGHT choice:**

```python
# GOOD: gather with return_exceptions for best-effort result collection
tool_tasks: dict[str, asyncio.Task[list[Tool]]] = {}
for name, entry in per_name.items():
    if isinstance(entry, RunningServerEntry):
        tool_tasks[name] = asyncio.create_task(entry.tools)

# Collect all results, even if some fail
results = await asyncio.gather(*tool_tasks.values(), return_exceptions=True)
for (name, _), result in zip(tool_tasks.items(), results):
    if isinstance(result, Exception):
        per_name[name] = FailedServerEntry(error=str(result))
    else:
        per_name[name] = RunningServerEntry(initialize=entry.initialize, tools=result)
```

**Why gather is better here:**

- Needs return values from each task
- Wants all results (best-effort), not fail-fast
- Simple, direct code without helper functions

#### Use TaskGroup when

1. **Fire-and-forget tasks** (no return values needed)
2. **Fail-fast behavior**: Want to cancel all tasks if any fails
3. **Manual result collection is acceptable**: Can store results in shared dict/list
4. **Python 3.11+** available

**Example - TaskGroup is the RIGHT choice:**

```python
# GOOD: TaskGroup for fail-fast fire-and-forget tasks
async def _notify_subscriber(subscriber: Subscriber, event: Event):
    await subscriber.notify(event)

async with asyncio.TaskGroup() as tg:
    for subscriber in subscribers:
        tg.create_task(_notify_subscriber(subscriber, event))
# If any notification fails, all others are cancelled and exception propagates
```

**Why TaskGroup is better here:**

- No return values needed (fire-and-forget)
- Want fail-fast behavior (one failure = abort all)
- Structured concurrency

#### Key Differences

| Feature                  | gather()                                    | TaskGroup                                     |
| ------------------------ | ------------------------------------------- | --------------------------------------------- |
| **Return values**        | ✅ Returns list of results                  | ❌ Requires manual collection                 |
| **Error handling**       | `return_exceptions=True` for best-effort    | Always fail-fast (first error cancels others) |
| **Exception on failure** | Only if `return_exceptions=False` (default) | Always raises first exception                 |
| **Task cancellation**    | No automatic cancellation on error          | Cancels all tasks on first error              |
| **Result order**         | Preserves input order                       | No built-in ordering                          |
| **Python version**       | All versions                                | 3.11+ only                                    |
| **Use case**             | Need results, best-effort execution         | Fire-and-forget, fail-fast                    |

#### When gather is MISUSED

**Anti-pattern: gather without return_exceptions when you need fail-fast**

```python
# BAD: Using gather's default fail-fast but ignoring result ordering
results = await asyncio.gather(task1(), task2(), task3())  # Fails on first error
# Problem: If fail-fast is desired, TaskGroup is clearer intent
```

**Better with TaskGroup for fail-fast:**

```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(task1())
    tg.create_task(task2())
    tg.create_task(task3())
```

#### Summary

- **gather**: Default choice when you need return values or best-effort execution
- **TaskGroup**: Use for fire-and-forget tasks or when fail-fast behavior is critical
- **Don't blindly replace gather with TaskGroup**: gather is often the right choice

**Detection:**

```bash
# Find gather() usage - review each for TaskGroup suitability
rg --type py 'asyncio\.gather\('
```

### 0.5. Context Managers for Resource Management + Cleanup

**Pattern**: Manual try-finally for resource cleanup when async context manager would centralize it.

**Applies to both sync and async code.**

```python
# BAD: Manual cleanup scattered across call sites
async def _proxy_stream(resp: httpx.Response, db: ResponsesDB, key: str, ...) -> AsyncIterator[bytes]:
    try:
        # ... streaming logic using db, key repeatedly ...
        await db.finalize_response(key, ...)
    except Exception as exc:
        await db.record_error(key, error_reason=str(exc), ...)
        raise
    finally:
        await resp.aclose()

async def event_stream():
    try:
        async for chunk in _proxy_stream(resp, db=db, key=key, ...):
            yield chunk
    finally:
        await client.aclose()

# GOOD: Context manager bundles state + handles cleanup
class StreamingContext:
    def __init__(self, db: ResponsesDB, key: str, *, client: httpx.AsyncClient | None = None):
        self.db = db
        self.key = key
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                await self.db.record_error(self.key, error_reason=str(exc_val), ...)
        finally:
            if self._client:
                await self._client.aclose()
        return False

    async def proxy_stream(self, resp: httpx.Response, ...) -> AsyncIterator[bytes]:
        try:
            # ... streaming logic using self.db, self.key ...
            await self.db.finalize_response(self.key, ...)
        finally:
            await resp.aclose()

async def event_stream():
    async with StreamingContext(db=db, key=key, client=client) as ctx:
        async for chunk in ctx.proxy_stream(resp, ...):
            yield chunk
```

**When to use context manager for bundling + cleanup:**

- 3+ parameters passed together repeatedly (db, key, response_id)
- Resource cleanup needed (client.aclose(), file.close())
- Error handling that must run regardless of success/failure
- Multiple related functions operate on same state

**Benefits:**

- **Single cleanup location**: Error handling in `__aexit__`, not scattered
- **Automatic resource cleanup**: Guaranteed via context manager protocol
- **Bundled parameters**: Avoid repeating (db, key, ...) at every call site
- **Object-oriented**: Methods on context vs standalone functions

**Detection:**

```bash
# Find try-finally with resource cleanup
rg --type py -U 'try:.*finally:.*\.(close|aclose)\(' --multiline

# Find repeated parameter bundles (candidates for context manager)
rg --type py 'def.*\(.*db.*key.*response'

# Find manual error recording patterns
rg --type py -U 'except.*:.*record_error' --multiline
```

**Sync version example:**

```python
# Sync context manager
class Transaction:
    def __init__(self, db: Database, isolation_level: str = "READ COMMITTED"):
        self.db = db
        self._isolation_level = isolation_level
        self._conn = None

    def __enter__(self):
        self._conn = self.db.begin_transaction(self._isolation_level)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False

    def execute(self, query: str, params: dict):
        return self._conn.execute(query, params)

# Usage
with Transaction(db) as tx:
    tx.execute("INSERT ...", {})
    tx.execute("UPDATE ...", {})
```

### 1. Unnecessary @pytest.mark.asyncio Decorators

**Context**: Projects can configure `asyncio_mode = "auto"` in `[tool.pytest.ini_options]` section of `pyproject.toml` (or `pytest.ini`), which automatically detects async test functions without requiring explicit `@pytest.mark.asyncio` decorators.

**Antipattern**: Using `@pytest.mark.asyncio` decorators when `asyncio_mode = "auto"` is configured.

**Fix Strategy**:

1. **Check pytest configuration**: Look for `asyncio_mode = "auto"` in project's `pyproject.toml` or `pytest.ini`
2. **If auto-detection is enabled**: Remove `@pytest.mark.asyncio` decorators - pytest will automatically detect `async def test_*()` functions
3. **If auto-detection is NOT enabled**: Consider enabling it by adding to `pyproject.toml`:

   ```toml
   [tool.pytest.ini_options]
   asyncio_mode = "auto"
   ```

   Then remove the decorators.

**Detection**:

```bash
# Step 1: Check if project has asyncio auto-detection
rg --type toml 'asyncio_mode.*=.*"auto"' pyproject.toml
rg 'asyncio_mode.*=.*auto' pytest.ini

# Step 2: If auto-detection is found, find redundant decorators
rg --type py '@pytest\.mark\.asyncio'

# Step 3: Review each async test to confirm it would be auto-detected
# (any `async def test_*()` function will be detected)
```

**Benefit**: Cleaner test code, automatic detection of new async tests without manual decorator addition.

### 1. Blocking I/O in Async Functions

- **File I/O**: `path.read_text()`, `path.write_text()`, `open()` without async wrappers
- **Subprocess**: `subprocess.run()`, `subprocess.Popen().communicate()` without await
- **Network**: `socket.connect()`, `socket.recv()`, `socket.send()` without async wrappers
- **Pipe/FD I/O**: `os.read()`, `os.write()` without non-blocking setup or async wrappers

**Fix**: Use `asyncio.create_subprocess_exec()`, `asyncio.open_connection()`, `asyncio.to_thread()`, or `aiofiles`

### 2. Deprecated APIs

- **`asyncio.get_event_loop()`**: Deprecated in Python 3.10+
- **Nested `asyncio.run()`**: Cannot be called from within a running event loop

**Fix**: Use `asyncio.get_running_loop()` instead; only use `asyncio.run()` at top-level entry points

### 3. Non-Blocking FD Issues

- **Blocking FDs with asyncio**: Must set `O_NONBLOCK` before using with `connect_read_pipe()`/`connect_write_pipe()`
- **`os.pipe()` without `fcntl` setup**: File descriptors are blocking by default

**Fix**: Use `fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)` before asyncio use

### 4. Missing Async Primitives

- **Python lacks `asyncio.open_pipe(fd)`**: No high-level API like `open_connection()` for file descriptors

**Fix**: Create helper following `asyncio.open_connection()` pattern (source says "just copy the code"):

```python
async def open_write_pipe(fd: int) -> asyncio.StreamWriter:
    import fcntl
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.connect_write_pipe(
        lambda: protocol, os.fdopen(fd, 'wb', buffering=0)
    )
    return asyncio.StreamWriter(transport, protocol, reader, loop)
```

## Detection Strategy

**MANDATORY Step 0**: Discover ALL async functions and await usage in the codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL async/await output using your intelligence
- High recall required, high precision NOT required - you determine which are problematic
- Review each for: blocking I/O, deprecated APIs, gather vs TaskGroup, error handling
- Prevents lazy analysis by forcing examination of ALL async code

```bash
# Find ALL async function definitions with context
rg --type py '^async def ' -B 1 -A 5 --line-number

# Find ALL await expressions with context
rg --type py '\bawait\b' -B 2 -A 1 --line-number

# Find asyncio.gather usage
rg --type py 'asyncio\.gather\(' -B 2 -A 2 --line-number

# Find TaskGroup usage
rg --type py 'TaskGroup\(' -B 2 -A 2 --line-number

# Find asyncio.create_task usage
rg --type py 'asyncio\.create_task\(' -B 2 -A 2 --line-number

# Count total async functions and await statements
echo "Async functions:" && rg --type py '^async def ' | wc -l
echo "Await statements:" && rg --type py '\bawait\b' | wc -l
```

**What to review for each async function:**

1. **Blocking I/O**: Path.read_text(), open(), subprocess.run() in async functions
2. **Deprecated APIs**: asyncio.ensure_future, @asynccontextmanager issues
3. **gather vs TaskGroup**: Does it need return values? Best-effort or fail-fast?
4. **Error handling**: Are exceptions properly handled in tasks?
5. **CPU-bound operations**: Long-running sync code that should use run_in_executor

**Process ALL output**: Read each async function, use your judgment to identify antipatterns.

---

**Primary Method**: Manual code reading of async functions to identify blocking operations.

**Why automation is insufficient**: Determining if an operation blocks requires understanding:

- Library implementation details (does this library use async I/O internally?)
- Whether operation is truly I/O-bound or CPU-bound
- Context: is `subprocess.run()` acceptable if it's truly fast and infrequent?

**Discovery aids AFTER Step 0** (candidates for manual review):

### Grep Patterns for Blocking I/O in async functions

```bash
# Find path.read_text/write_text in async functions
rg --type py -U 'async def.*\n.*\n.*\.(read_text|write_text|read_bytes|write_bytes)'

# Find open() in async functions
rg --type py -U 'async def.*\n.*\n.*\bopen\('

# Find os.read/os.write in async functions
rg --type py -U 'async def.*\n.*\n.*os\.(read|write)\('

# Find subprocess.run in async functions
rg --type py -U 'async def.*\n.*\n.*subprocess\.run\('

# Find subprocess.Popen in async functions
rg --type py -U 'async def.*\n.*\n.*subprocess\.Popen\('
```

### Deprecated APIs

```bash
# Find get_event_loop() usage (deprecated)
rg --type py 'asyncio\.get_event_loop\(\)'

# Find asyncio.run() outside main entry points
rg --type py 'asyncio\.run\(' | grep -v '__main__' | grep -v '^if __name__'
```

### Non-blocking FD issues

```bash
# Find os.fdopen without O_NONBLOCK nearby
rg --type py 'os\.fdopen\(' -A5 -B5 | grep -L 'O_NONBLOCK'

# Find os.pipe() without O_NONBLOCK setup
rg --type py 'os\.pipe\(\)' -A10 | grep -L 'O_NONBLOCK'

# Find connect_read_pipe/connect_write_pipe usage
rg --type py 'connect_(read|write)_pipe'
```

### Socket operations in async

```bash
# Find socket operations in async functions
rg --type py -U 'async def.*\n.*\n.*(socket\..*\.connect|socket\..*\.recv|socket\..*\.send)\('
```

## Fix Strategy

1. **Identify blocking I/O**: Any file, network, or subprocess operation
2. **Choose async primitive**:
   - **File I/O**: `aiofiles` or `asyncio.to_thread(path.read_text)`
   - **Subprocess**: `asyncio.create_subprocess_exec()` (never `subprocess.run()`)
   - **Network**: `asyncio.open_connection()` / `asyncio.open_unix_connection()`
   - **Pipe/FD I/O**: Create `open_pipe()` helper or `asyncio.to_thread()` for one-shot
   - **CPU-bound**: `asyncio.to_thread()` or `ProcessPoolExecutor`
3. **Set FDs to non-blocking**: Use `fcntl` to set `O_NONBLOCK` before asyncio use
4. **Use modern APIs**: Replace `get_event_loop()` with `get_running_loop()`
5. **Never nest asyncio.run()**: Only use in top-level entry points

### Preference Hierarchy

1. **Native asyncio** (e.g., `create_subprocess_exec`, `open_connection`, custom `open_pipe()`)
   - True async I/O, no thread overhead
2. **`asyncio.to_thread()`** (for unavoidable blocking operations)
   - When no native asyncio alternative exists
   - For quick/infrequent blocking operations
3. **Never**: Direct blocking calls in async functions

## References

- [Python asyncio documentation](https://docs.python.org/3/library/asyncio.html)
- [asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)
- [Event Loop APIs](https://docs.python.org/3/library/asyncio-eventloop.html)
- [Ruff ASYNC rules](https://docs.astral.sh/ruff/rules/#flake8-async-async)
