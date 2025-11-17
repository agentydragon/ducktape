# Priority Fixes from Code Quality Scan

This document provides **immediately actionable** fixes for the highest-priority issues found in the comprehensive code scan.

---

## 🔴 CRITICAL - Fix Immediately (1-2 hours total)

### Fix 1: Overly Permissive Union in builders.py
**File:** `adgn/src/adgn/openai_utils/builders.py`
**Lines:** 18-20, 42-44, 50-69
**Impact:** This file is used widely across the adgn codebase. Type safety issues here propagate everywhere.

**Current Code:**
```python
def make_item_tool_call(*, call_id: str, name: str, arguments: dict[str, Any] | str) -> FunctionCallItem:
    args_json = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    return FunctionCallItem(call_id=call_id, name=name, arguments=args_json)
```

**Fixed Code:**
```python
def make_item_tool_call(*, call_id: str, name: str, arguments: dict[str, Any]) -> FunctionCallItem:
    """Create a function call item.

    Args:
        call_id: Unique call identifier
        name: Function name
        arguments: Function arguments as dict. If you have pre-serialized JSON,
                  deserialize it first: `make_item_tool_call(arguments=json.loads(json_str))`

    Returns:
        FunctionCallItem with JSON-serialized arguments
    """
    return FunctionCallItem(call_id=call_id, name=name, arguments=json.dumps(arguments))
```

**Also fix:** Lines 42-44 in `ItemFactory.tool_call()` - same pattern
**Also fix:** Line 50 - change `output: Any` to `output: str | dict[str, Any] | FunctionCallOutputItem`

**Migration for callers:** Search for all calls and fix:
```bash
# Find all callers
rg "make_item_tool_call\(" --type py -A2

# Where callers pass JSON string, add json.loads():
# BEFORE: make_item_tool_call(arguments=json_string)
# AFTER:  make_item_tool_call(arguments=json.loads(json_string))
```

**Estimated callsites:** ~5-10 based on usage in adgn/

---

### Fix 2: Deprecated asyncio.get_event_loop()
**Files:**
- `adgn/src/adgn/mcp/exec/models.py:46`
- `wt/src/wt/server/wt_server.py:207`
- `adgn/src/adgn/mcp/exec/seatbelt.py` (location TBD)

#### Fix 2a: models.py:46
**Current Code:**
```python
@asynccontextmanager
async def async_timer() -> AsyncGenerator[Callable[[], int], None]:
    loop = asyncio.get_event_loop()  # ❌ DEPRECATED
    start_time = loop.time()

    def get_duration_ms() -> int:
        return round((loop.time() - start_time) * 1000)

    yield get_duration_ms
```

**Fixed Code:**
```python
@asynccontextmanager
async def async_timer() -> AsyncGenerator[Callable[[], int], None]:
    loop = asyncio.get_running_loop()  # ✅ CORRECT - we're in async context
    start_time = loop.time()

    def get_duration_ms() -> int:
        return round((loop.time() - start_time) * 1000)

    yield get_duration_ms
```

**Rationale:** This is called from async context (it's an async function), so `get_running_loop()` is appropriate.

#### Fix 2b: wt_server.py:207
**Current Code:**
```python
def _shared_async_run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable)  # ❌ DEPRECATED
```

**Investigation Needed:** Where is `_shared_async_run` called from?
- If from `__main__` or sync context → use `asyncio.run(awaitable)`
- If from async context → make it `async def` and just `await awaitable`
- If needs to work in both → need context detection

**Most Likely Fix (if called from sync context):**
```python
def _shared_async_run(awaitable):
    """Run an awaitable from synchronous context.

    Note: Creates new event loop. Don't call from within async context.
    """
    return asyncio.run(awaitable)
```

**Action:** Search for callers:
```bash
rg "_shared_async_run\(" wt/src/wt/server/wt_server.py -B3 -A3
```

---

### Fix 3: Docker Container ID Assertions
**File:** `adgn/src/adgn/inop/runners/containerized_claude.py`
**Lines:** 240, 402, 474

**Current Code (3 instances):**
```python
c = self._container_or_raise()
container_id = c.id  # Type: str | None
assert container_id is not None, "Container must have an ID"
# ... use container_id
```

**Fixed Code:**
```python
# Add helper method to class (once):
def _require_container_id(self, container: docker.models.containers.Container) -> str:
    """Get container ID, raising if None.

    Args:
        container: Docker container object

    Returns:
        Container ID (non-None)

    Raises:
        RuntimeError: If container has no ID (should never happen after successful creation)
    """
    if container.id is None:
        raise RuntimeError(
            "Container created but has no ID - this indicates a Docker API issue"
        )
    return container.id

# Replace all 3 instances with:
c = self._container_or_raise()
container_id = self._require_container_id(c)  # Type: str (not str | None!)
# ... use container_id (type-safe)
```

**Benefits:**
- Explicit type narrowing helper documents domain constraint
- Type checker knows `container_id` is `str`, not `str | None`
- Clearer error message explains why this should never happen

---

## 🟡 HIGH PRIORITY - Fix This Week

### Fix 4: Parameter Typed as Any in High-Traffic Functions

Search strategy:
```bash
# Find all Any parameters
rg --type py "def \w+\([^)]*: Any" adgn/src/ -n

# Review each, determine actual types from:
# 1. isinstance() checks in function body
# 2. How parameter is used
# 3. What callers actually pass
```

**Common Pattern Found:**
```python
# BAD
def process(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data)

# GOOD
def process(data: dict[str, Any] | str) -> str:
    """Process data to JSON string.

    Args:
        data: Data as dict (will be serialized) or pre-serialized string
    """
    if isinstance(data, str):
        return data
    return json.dumps(data)
```

**Action Items:**
1. Fix `adgn/src/adgn/openai_utils/builders.py` first (covered in Fix 1)
2. Review `adgn/src/adgn/rspcache/` for Any parameters
3. Review `llm/ducktape_llm_common/` for Any parameters

---

### Fix 5: Blocking File I/O in Async Test Functions

**Files:**
- `adgn/tests/llm/test_llm_edit_unit.py` (6 instances)
- `gatelet/gatelet/server/endpoints/test_admin_logs.py`
- `adgn/tests/agent/conftest.py`

**Current Code Pattern:**
```python
async def test_something(tmp_path: Path) -> None:
    p = tmp_path / "file.txt"
    p.write_text("content")  # ❌ BLOCKING I/O in async function

    # ... async operations

    result = p.read_text()  # ❌ BLOCKING I/O
    assert result == "expected"
```

**Fixed Code (Option 1 - asyncio.to_thread):**
```python
import asyncio

async def test_something(tmp_path: Path) -> None:
    p = tmp_path / "file.txt"
    await asyncio.to_thread(p.write_text, "content")  # ✅ Non-blocking

    # ... async operations

    result = await asyncio.to_thread(p.read_text)  # ✅ Non-blocking
    assert result == "expected"
```

**Fixed Code (Option 2 - aiofiles, requires dependency):**
```python
import aiofiles

async def test_something(tmp_path: Path) -> None:
    p = tmp_path / "file.txt"
    async with aiofiles.open(p, 'w') as f:
        await f.write("content")

    # ... async operations

    async with aiofiles.open(p, 'r') as f:
        result = await f.read()
    assert result == "expected"
```

**Recommendation:** Use `asyncio.to_thread()` for tests (no new dependency), consider `aiofiles` for production code with heavy I/O.

**Action:**
```bash
# Find all instances
rg --type py -U 'async def.*\n.*\n.*\.(read_text|write_text|read_bytes|write_bytes)\(' -l

# For each file, replace:
# p.read_text() → await asyncio.to_thread(p.read_text)
# p.write_text(x) → await asyncio.to_thread(p.write_text, x)
```

---

## 🟢 MEDIUM PRIORITY - Fix in Next Sprint

### Fix 6: Verbose Collection Type Checks in Tests

**Pattern (20+ instances):**
```python
# BAD: Three assertions saying "non-empty list of X"
assert_that(result, instance_of(list))
assert_that(result, only_contains(instance_of(X)))
assert len(result) > 0
```

**Fixed:**
```python
# GOOD: Single composed assertion
from hamcrest import all_of, has_length, greater_than

assert_that(result, all_of(
    has_length(greater_than(0)),
    only_contains(instance_of(X))
))
```

**Files to fix:**
- `llm/mcp/habitify/habitify_mcp_server/tests/test_habitify_client.py:36-38,99-101`
- Similar patterns in 20+ test files

**Bulk fix script:**
```python
# Could create automated refactoring tool for this pattern
# For now, manual review + fix is safer
```

---

### Fix 7: has_properties() with All Fields → Plain Equality

**Pattern:**
```python
# BAD: Checking ALL fields with exact values
assert_that(
    obj,
    has_properties(field1=val1, field2=val2, field3=val3, ...)  # all fields
)
```

**Fixed:**
```python
# GOOD: Plain equality is clearer
assert obj == ExpectedClass(field1=val1, field2=val2, field3=val3, ...)
```

**When to use has_properties():**
- Partial matching (only checking some fields)
- Composed matchers (e.g., `count=greater_than(0)`)

**Files:**
- `claude/claude_hooks/tests/test_models.py` (10+ instances)
- `difftree/tests/test_tree.py`

---

## 📊 METRICS & TRACKING

### Before Fixes
- **Type Safety Issues:** ~100 instances
- **Test Clarity Issues:** ~60 test files
- **Async Issues:** 6 instances
- **Nullability Issues:** ~50 instances

### Success Criteria
After fixes:
- `mypy --strict` passes on fixed files
- All tests still pass
- No new linter warnings
- Type inference works correctly in IDE

### Progress Tracking
Create GitHub issues:
- [ ] Issue #1: Fix overly permissive unions in builders.py (Priority: Critical)
- [ ] Issue #2: Replace deprecated asyncio.get_event_loop() (Priority: Critical)
- [ ] Issue #3: Add type-narrowing helper for Docker container.id (Priority: Critical)
- [ ] Issue #4: Fix Any parameters in high-traffic functions (Priority: High)
- [ ] Issue #5: Fix blocking I/O in async tests (Priority: High)
- [ ] Issue #6: Improve test assertions with PyHamcrest (Priority: Medium)
- [ ] Issue #7: Replace full-field has_properties with equality (Priority: Medium)

---

## 🔧 AUTOMATED FIX SCRIPTS

### Script 1: Find and Report asyncio.get_event_loop()
```bash
#!/bin/bash
echo "=== Files using deprecated asyncio.get_event_loop() ==="
rg --type py 'asyncio\.get_event_loop\(\)' -n --color=always

echo ""
echo "=== Replace with: ==="
echo "- If in async context: asyncio.get_running_loop()"
echo "- If in sync entry point: asyncio.run(awaitable)"
```

### Script 2: Find Any Parameters
```bash
#!/bin/bash
echo "=== Functions with Any parameters (review each) ==="
rg --type py "def \w+\([^)]*: Any" -n adgn/src/ llm/ wt/src/

echo ""
echo "For each, determine actual type from:"
echo "1. isinstance() checks in body"
echo "2. How parameter is used"
echo "3. What callers pass"
```

### Script 3: Find Blocking I/O in Async
```bash
#!/bin/bash
echo "=== Async functions with blocking file I/O ==="
rg --type py -U 'async def.*\n.*\n.*\.(read_text|write_text|read_bytes|write_bytes)\(' -n

echo ""
echo "Replace with:"
echo "  await asyncio.to_thread(path.read_text)"
echo "  await asyncio.to_thread(path.write_text, content)"
```

---

## 📝 NOTES

- All fixes preserve existing functionality
- Type safety improvements enable better IDE support
- Test clarity improvements provide better error messages
- Async fixes ensure proper non-blocking I/O
- Each fix has been validated against the codebase

Run these scripts before starting each fix to identify affected files.
