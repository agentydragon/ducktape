# Scan: Error Swallowing (Let It Crash)

## Context

@../shared-context.md

## Pattern Description

Error swallowing occurs when exceptions are caught but not re-raised, causing failures to be hidden instead of propagating. This violates the "fail fast, fail visibly" principle - when the application cannot successfully continue, it should crash rather than silently degrade.

**Core Principle**: Let it crash. If infrastructure fails or critical invariants are violated, the system should stop immediately rather than continue in a degraded state.

## Why Error Swallowing is Problematic

- **Hidden Failures**: Critical errors go unnoticed, making debugging impossible
- **Degraded State**: Application continues with broken infrastructure
- **Cascading Issues**: Initial failure causes secondary problems elsewhere
- **Silent Data Loss**: Operations fail without notification
- **False Health Reports**: System appears healthy while broken

## Philosophy: When to Catch vs Let Crash

### Let It Crash (Don't Catch)

- **Infrastructure failures**: Database connection lost, file system full, out of memory
- **Programming errors**: Assertion violations, type errors, null pointer dereferences
- **Configuration errors**: Missing required config, invalid credentials
- **Dependency failures**: External service permanently unavailable
- **Invariant violations**: Data corruption, impossible state transitions

### Catch Gracefully (User-Facing Errors)

- **User input errors**: Invalid data format, missing required fields
- **Transient failures**: Network timeout (with retry), temporary service unavailability
- **Expected business logic**: Permission denied, resource not found
- **Validation failures**: Schema mismatch, constraint violation

**Key Distinction**: Can the system continue to function correctly after this error? If no → crash. If yes → handle gracefully.

## Common Antipatterns

### Pattern 1: Logging and Continuing

**BAD**: Infrastructure failure logged but ignored

```python
async def get_channel_bundle(app: FastAPI, agent_id: str) -> ChannelBundle | None:
    await app.state.ready.wait()
    try:
        runtime = await app.state.registry.ensure_live(agent_id, with_ui=True)
    except KeyError:
        return None
    except Exception as e:
        logger.exception("ensure_live failed", exc_info=e)
        return None  # ❌ Swallows infrastructure failure

    # ... continues with potentially broken state
```

**Issue**: `ensure_live()` failing indicates infrastructure is broken, but we continue anyway.

**GOOD**: Only catch expected errors, let infrastructure failures crash

```python
async def get_channel_bundle(app: FastAPI, agent_id: str) -> ChannelBundle:
    """Get or create channel bundle for agent.

    Raises KeyError if agent doesn't exist.
    Raises other exceptions if ensure_live fails.
    """
    await app.state.ready.wait()
    runtime = await app.state.registry.ensure_live(agent_id, with_ui=True)

    # Let infrastructure failures crash
    # Only handle expected KeyError at call site if needed

    if runtime._channel_bundle is None:
        runtime._channel_bundle = ChannelBundle.for_agent_runtime(runtime)
    return runtime._channel_bundle
```

**Call site handling** (if user-facing):

```python
try:
    bundle = await get_channel_bundle(app, agent_id)
except KeyError:
    await send_error(ws, "Agent not found")  # User error, handle gracefully
    return
except Exception:
    await send_error(ws, "System error")
    raise  # ✅ Re-raise infrastructure failure
```

### Pattern 2: Returning None on All Errors

**BAD**: Treating all errors as "not found"

```python
def get_config(key: str) -> Config | None:
    try:
        return load_config_from_db(key)
    except Exception:
        return None  # ❌ Hides DB connection failure, parse errors, etc.
```

**Issue**: Caller can't distinguish between "config doesn't exist" and "database is down".

**GOOD**: Let infrastructure failures crash, only return None for legitimate absence

```python
def get_config(key: str) -> Config | None:
    """Get config by key.

    Returns None if config doesn't exist.
    Raises if database is unavailable or config is corrupted.
    """
    try:
        return load_config_from_db(key)
    except ConfigNotFoundError:
        return None  # Expected: config doesn't exist
    # Let other exceptions propagate:
    # - DatabaseConnectionError: infrastructure failure
    # - ConfigParseError: data corruption
    # - ValidationError: schema violation
```

### Pattern 3: Catching and Logging Without Re-raising

**BAD**: Notification failure silently absorbed

```python
for agent_id in registry.known_agents():
    try:
        infra = await registry.get_infrastructure(agent_id)
        infra.approval_engine.set_notifier(make_notifier(agent_id))
    except Exception as e:
        logger.warning(f"Failed to hook listeners for {agent_id}: {e}")
        # ❌ Continues to next agent, system partially broken
```

**Issue**: If infrastructure setup fails, the system is broken. Continuing silently creates unpredictable behavior.

**GOOD**: Let infrastructure failures crash

```python
for agent_id in registry.known_agents():
    infra = await registry.get_infrastructure(agent_id)
    # If this fails, we want to know immediately - system can't function
    infra.approval_engine.set_notifier(make_notifier(agent_id))
```

### Pattern 4: Swallowing Errors in Event Loop Notifier

**BAD**: Missing event loop treated as non-critical

```python
def notifier(uri: str):
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(server.broadcast_resource_updated(uri))
    except RuntimeError:
        logger.warning(f"Could not broadcast {uri}: no running event loop")
        # ❌ Notification system broken, continues silently
```

**Issue**: If there's no event loop, the notification system is fundamentally broken. This is a programming error.

**GOOD**: Let it crash - no event loop is a bug

```python
def notifier(uri: str):
    # If there's no event loop, we WANT this to crash
    # This is a programming error, not a recoverable condition
    loop = asyncio.get_running_loop()
    loop.create_task(server.broadcast_resource_updated(uri))
```

### Pattern 5: Contextlib.suppress for Critical Operations

**BAD**: Using `suppress()` for operations that must succeed

```python
with contextlib.suppress(Exception):
    await critical_database_operation()
    # ❌ If this fails, we want to know!
```

**ACCEPTABLE**: Using `suppress()` only for truly optional operations

```python
# Sending final error message to client on disconnection
with contextlib.suppress(Exception):
    await ws.send_json(error_envelope)
    # ✅ OK: Client may have disconnected, this is best-effort
```

**Rule**: Only use `suppress()` when the operation is genuinely optional and failure doesn't affect correctness.

## Detection Strategy

**MANDATORY first step**: Run `scan_error_handling.py` and process ALL output.

- This scan is **required** - do not skip this step
- You **must** read and handle the complete scan output (can pipe to temp file)
- Do not sample or skip any results - process every exception handler found
- Prevents lazy analysis by forcing examination of all try-except blocks in the codebase

### Automated Scanning Tool

**Tool**: `prompts/scans/scan_error_handling.py` - AST-based scanner for error handling antipatterns

**What it finds**:

1. **Bare except** - `except:` with no exception type
2. **Broad except** - `except Exception:` (too broad)
3. **Non-raising except** - Exception handlers that don't re-raise (return, pass, etc.)
4. **Single-line try** - Try-except wrapping single statement (use contextlib.suppress)

**Usage**:

```bash
# Run on entire codebase
python prompts/scans/scan_error_handling.py . > error_handling_scan.json

# Filter for high-priority issues (bare except)
cat error_handling_scan.json | jq '.issues | to_entries[] |
  {file: .key, bare_except: .value.bare_except}'
```

**Output structure**:

- `summary`: Counts of each issue type
- `issues`: Dict mapping file paths to lists of issues by type:
  - `bare_except`: `{line, col}` for each bare except
  - `broad_except`: `{line, col}` for each `except Exception:`
  - `non_raising_except`: `{line, col}` for handlers without raise
  - `single_line_try`: `{line, col}` for single-line try blocks

**Tool characteristics**:

- **~100% recall**: Finds all try-except patterns
- **High false positives for non_raising_except**: Includes legitimate logging before re-raise
- **Expected**: You review each finding in context

**Example output**:

```json
{
  "summary": { "bare_except": 5, "broad_except": 12, "non_raising_except": 34 },
  "issues": {
    "src/client.py": {
      "bare_except": [{ "line": 45, "col": 4 }],
      "non_raising_except": [
        { "line": 45, "col": 4 },
        { "line": 67, "col": 4 }
      ]
    }
  }
}
```

### Grep Patterns (Supplement)

Manual grep patterns for additional context:

```bash
# Find contextlib.suppress usage (review each)
rg --type py 'contextlib\.suppress\('

# Find except blocks that log but don't re-raise
rg --type py -U 'except.*:.*\n.*logger\.(warning|error|exception).*\n(?!.*raise)' --multiline

# Find except blocks returning None
rg --type py -U 'except.*:.*\n.*return None' --multiline
```

### Examples to Flag

**High Priority** (likely wrong):

```python
# Pattern: Infrastructure failure returning None
try:
    result = await critical_operation()
except Exception:
    logger.error("Operation failed")
    return None

# Pattern: Setup failure continuing silently
try:
    await initialize_system()
except Exception as e:
    logger.warning(f"Init failed: {e}")

# Pattern: Notification failure absorbed
try:
    await notify_subscribers()
except Exception:
    pass  # ❌ Silent failure
```

**Medium Priority** (review carefully):

```python
# Pattern: Broad exception catch with logging
try:
    resource = await load_resource()
except Exception as e:
    logger.exception("Failed to load resource")
    return fallback  # ⚠️ Is fallback safe?

# Pattern: Multiple exception types with same handler
except (TypeError, ValueError, KeyError):
    return None  # ⚠️ Are all these truly "not found"?
```

**Low Priority** (likely acceptable):

```python
# Pattern: User-facing validation
try:
    config = parse_user_config(data)
except ValidationError as e:
    return {"error": str(e)}  # ✅ User error, handled gracefully

# Pattern: Best-effort cleanup
try:
    await ws.close()
except Exception:
    pass  # ✅ OK: Connection may already be closed
```

## Fix Strategy

For each swallowed error:

1. **Identify the error type**:
   - Infrastructure failure? → Let it crash
   - User error? → Handle gracefully
   - Transient failure? → Consider retry logic

2. **Determine if system can continue**:
   - No → Remove catch, let it propagate
   - Yes with degraded state → Re-evaluate if degraded state is acceptable
   - Yes with full functionality → Handle specifically

3. **Choose appropriate fix**:
   - **Best**: Remove try-except entirely
   - **Good**: Catch specific exceptions, re-raise others
   - **Acceptable**: Catch, send error to user, then re-raise

4. **Update return types**:
   - Remove `| None` if errors now propagate
   - Add exception documentation to docstring

5. **Update call sites**:
   - Let crashes propagate by default
   - Only add try-except at appropriate boundaries (HTTP handlers, WebSocket handlers)

## Common Legitimate Catches

Some error catches are correct and should remain:

### Best-Effort I/O Cleanup

```python
finally:
    with contextlib.suppress(Exception):
        await ws.close()  # ✅ Connection may already be closed
```

### User Input Validation

```python
try:
    data = parse_user_input(request.json)
except ValidationError as e:
    return {"error": str(e)}, 400  # ✅ User error, not infrastructure
```

### Transient Failures with Retry

```python
for attempt in range(max_retries):
    try:
        return await flaky_api_call()
    except RequestTimeout:
        if attempt == max_retries - 1:
            raise  # ✅ Re-raise after max retries
        await asyncio.sleep(backoff_delay)
```

### Resource Existence Checks

```python
try:
    config = db.get(key)
except NotFoundError:
    config = create_default_config()  # ✅ Specific, expected exception
```

## Examples from Codebase

### Example 1: Channel Bundle Error Swallowing (FIXED)

```python
# ❌ BEFORE: Swallowed infrastructure failures
async def get_channel_bundle(app: FastAPI, agent_id: str) -> ChannelBundle | None:
    try:
        runtime = await app.state.registry.ensure_live(agent_id, with_ui=True)
    except KeyError:
        return None
    except Exception as e:
        logger.exception("ensure_live failed", exc_info=e)
        return None  # Swallowed!

# ✅ AFTER: Only catch expected errors
async def get_channel_bundle(app: FastAPI, agent_id: str) -> ChannelBundle:
    """Raises KeyError if agent doesn't exist, other exceptions if ensure_live fails."""
    runtime = await app.state.registry.ensure_live(agent_id, with_ui=True)
    # Infrastructure failures crash, KeyError handled at call site
    if runtime._channel_bundle is None:
        runtime._channel_bundle = ChannelBundle.for_agent_runtime(runtime)
    return runtime._channel_bundle
```

### Example 2: Notification Wiring Error Swallowing (FIXED)

```python
# ❌ BEFORE: Infrastructure setup failure hidden
for agent_id in registry.known_agents():
    try:
        infra = await registry.get_infrastructure(agent_id)
        infra.approval_engine.set_notifier(make_notifier(agent_id))
    except Exception as e:
        logger.warning(f"Failed to hook listeners for {agent_id}: {e}")

# ✅ AFTER: Let infrastructure failures crash
for agent_id in registry.known_agents():
    infra = await registry.get_infrastructure(agent_id)
    infra.approval_engine.set_notifier(make_notifier(agent_id))
```

### Example 3: Event Loop Error Swallowing (FIXED)

```python
# ❌ BEFORE: Missing event loop treated as recoverable
def notifier(uri: str):
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(server.broadcast_resource_updated(uri))
    except RuntimeError:
        logger.warning(f"Could not broadcast {uri}: no running event loop")

# ✅ AFTER: No event loop is a programming error - crash
def notifier(uri: str):
    loop = asyncio.get_running_loop()  # Crashes if no loop - good!
    loop.create_task(server.broadcast_resource_updated(uri))
```

## Verification Process

For each flagged pattern:

1. **Read the code** to understand what operation is failing
2. **Classify the error**:
   - Infrastructure failure → Remove catch
   - User error → Keep specific catch
   - Transient failure → Add retry or re-raise
3. **Check call sites**: Will crash reach appropriate error boundary?
4. **Update types**: Remove `| None` if errors now propagate
5. **Run tests**: Ensure system fails appropriately
6. **Verify error messages**: Are failures visible and debuggable?

## Priority for Fixing

**Critical** (fixes first):

- Infrastructure setup (database, event loop, registry)
- Notification/broadcasting systems
- Critical path operations (request handling, state mutations)

**High Priority**:

- Resource loading (configs, database records)
- API call forwarding
- Background task scheduling

**Medium Priority**:

- Cleanup operations that should succeed
- Optional features that should fail visibly
- Diagnostic/monitoring operations

**Low Priority** (may be correct):

- Best-effort cleanup in finally blocks
- User input validation
- Transient failure retry logic

## Summary

**Golden Rules**:

1. **Let it crash**: Infrastructure failures should propagate
2. **Fail fast, fail visibly**: Don't hide errors behind logs
3. **Only catch specific exceptions**: `except KeyError`, not `except Exception`
4. **Re-raise after handling**: If you log, re-raise
5. **Document exceptions**: Docstrings should list what can be raised

**Default stance**: Don't catch. Only add try-except when you have a specific, justifiable reason.

## Validation

After removing error swallowing:

```bash
# Type checker should pass (may need to update return types)
mypy --strict path/to/file.py

# Tests should still pass (or fail in better ways)
pytest path/to/tests/

# Run the application - verify crashes are visible
# Check that critical failures stop the system
# Verify user-facing errors still handled gracefully
```

**Expected outcome**: System crashes fast on infrastructure failures, making issues visible and debuggable.
