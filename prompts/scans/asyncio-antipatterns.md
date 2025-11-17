# Scan: Asyncio Antipatterns

## Context
@../shared-context.md

## Common Antipatterns

### 0. Unnecessary @pytest.mark.asyncio Decorators

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

**Primary Method**: Manual code reading of async functions to identify blocking operations.

**Why automation is insufficient**: Determining if an operation blocks requires understanding:
- Library implementation details (does this library use async I/O internally?)
- Whether operation is truly I/O-bound or CPU-bound
- Context: is `subprocess.run()` acceptable if it's truly fast and infrequent?

**Discovery aids** (candidates for manual review):

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
