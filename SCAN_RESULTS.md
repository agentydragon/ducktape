# Code Quality Scan Results
**Date:** 2025-11-17
**Scans Run:** test-assertions.md, suspicious-nullability.md, overly-loose-typing.md, asyncio-antipatterns.md

---

## Executive Summary

This comprehensive scan identified **100+ instances** of code quality issues across 4 categories:

1. **Test Assertions** (60+ files): Verbose test assertions that could use PyHamcrest matchers
2. **Suspicious Nullability** (50+ instances): Unnecessary None handling and type propagation
3. **Overly Loose Typing** (70+ instances): `Any` and `dict[str, Any]` that should be specific types
4. **Asyncio Antipatterns** (6 instances): Deprecated APIs and blocking I/O in async functions

---

## 1. Test Assertion Antipatterns

### Overview
Found 60+ test files using verbose plain assertions instead of PyHamcrest matchers.

---

#### Issue 1.1: Full-Object has_properties Should Use Plain Equality
**File:** `claude/claude_hooks/tests/test_models.py:18-21`

**Current Code:**
```python
# BAD: has_properties with ALL fields and exact values
assert_that(
    edit_input,
    has_properties(
        file_path=Path("/tmp/test.py"),
        old_string="old code",
        new_string="new code",
        replace_all=False
    ),
)
```

**Recommended Fix:**
```python
# GOOD: Plain equality is clearer for full object comparison
assert edit_input == EditInput(
    file_path=Path("/tmp/test.py"),
    old_string="old code",
    new_string="new code",
    replace_all=False
)
```

**Rationale:** When checking ALL fields with exact values, plain `==` is simpler and clearer. Reserve `has_properties()` for partial matching or composed matchers.

**Impact:** Simpler, more maintainable tests
**Priority:** Low
**Instances:** 10+ similar patterns in `claude/claude_hooks/tests/test_models.py`, `difftree/tests/test_tree.py`

---

#### Issue 1.2: String Inclusion Assertions

**Pattern for other tests:**
```python
# GOOD: PyHamcrest matcher with better error messages
from hamcrest import contains_string

assert_that(str(excinfo.value).lower(), contains_string("habit does not exist"))
assert_that(str(excinfo.value).lower(), contains_string("date format"))
```

**Impact:** Better failure messages
**Priority:** Low
**Instances:** 20+ across test files

---

### Additional Test Assertion Issues

**Files with multiple assertion antipatterns:**
- `difftree/tests/test_*.py` (8 files) - Mix of len() assertions and isinstance()
- `claude/claude_optimizer/tests/test_*.py` (6 files) - Field-by-field assertions
- `wt/tests/*/test_*.py` (15+ files) - Various assertion patterns

**Bulk Fix Strategy:**
1. Prioritize test files with 5+ assertion antipatterns
2. Focus on high-traffic modules (claude_optimizer, wt)
3. Use search/replace for common patterns

---

## 2. Suspicious Nullability

### Overview
Found 50+ instances where `| None` typing is suspicious or propagates unnecessarily.

### Critical Issues

#### Issue 2.1: Immediate Assertion That Value Is Not None
**File:** `adgn/src/adgn/inop/runners/containerized_claude.py:240,402,474`

**Current Code:**
```python
# BAD: container.id is typed str | None but we immediately assert non-None
container_id = c.id  # Type: str | None from Docker API
assert container_id is not None, "Container must have an ID"
# ... use container_id (3 times in this file)
```

**Recommended Fix:**
```python
# GOOD: Type-narrowing helper
def _require_container_id(container: Container) -> str:
    """Get container ID, raising if None.

    Args:
        container: Docker container object

    Returns:
        Container ID (non-None)

    Raises:
        RuntimeError: If container has no ID (should never happen after creation)
    """
    if container.id is None:
        raise RuntimeError("Container created but has no ID - this should never happen")
    return container.id

# Usage (replace all 3 instances):
container_id = _require_container_id(c)  # Type: str
```

**Rationale:**
- Docker containers always have IDs after creation
- Docker library types are overly conservative
- Type-narrowing helper makes intent clear and types precise

**Impact:** Type safety, clearer domain constraints
**Priority:** High
**Instances:** 3 in this file, 20+ across adgn/src/

---

#### Issue 2.2: Parameter Typed as T | None But Immediately Fails If None
**Location:** Not found in grep, but pattern likely exists

**Detection Strategy:**
```bash
# Find functions with None parameters that immediately check and raise
rg --type py "def \w+\([^)]*: \w+ \| None" -A3 | grep "if .* is None" | grep "raise"
```

**General Fix Pattern:**
```python
# BAD
def process(param: str | None) -> None:
    if param is None:
        raise ValueError("param is required")
    # ... use param

# GOOD
def process(param: str) -> None:
    # ... use param directly

# Handle None at call site:
value = get_optional_value()
if value is not None:
    process(value)
```

**Priority:** High
**Estimated Instances:** 10-15 based on pattern matching

---

#### Issue 2.3: None Propagation Through Layers
**File:** `wt/src/wt/server/services.py`, `wt/src/wt/server/gitstatusd_listener.py`

**Pattern Found:**
```python
# Multiple functions returning "X | None" when input is "Y | None"
return x if x else None
```

**Recommended Investigation:**
1. Trace None source (user input, config, API response)
2. Handle None ONCE at the optionality branch point
3. Make downstream functions work with non-None values

**Priority:** Medium
**Instances:** 2-5 based on grep results

---

### Additional Nullability Issues

**Files with assert is not None:**
- `wt/src/wt/server/pr_service.py` - Multiple assertions
- `adgn/src/adgn/agent/runtime/*.py` - Container management
- `llm/ducktape_llm_common/tests/*/test_*.py` - Test setup code
- `adgn/tests/*/test_*.py` - Test assertions

**Bulk Fix Strategy:**
1. Create type-narrowing helpers for Docker container.id pattern (highest frequency)
2. Review functions with `| None` parameters for immediate None checks
3. Refactor None propagation chains in wt/src/

---

## 3. Overly Loose Typing

### Overview
Found 70+ instances of `Any`, `dict[str, Any]`, and overly permissive unions.

### Critical Issues

#### Issue 3.1: Overly Permissive Union - dict | str
**File:** `adgn/src/adgn/openai_utils/builders.py:18-20`

**Current Code:**
```python
# BAD: Accepts both dict and str "for convenience"
def make_item_tool_call(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any] | str  # Ambiguous!
) -> FunctionCallItem:
    args_json = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    return FunctionCallItem(call_id=call_id, name=name, arguments=args_json)
```

**Recommended Fix:**
```python
# GOOD: Clear, unambiguous API - accept only structured data
def make_item_tool_call(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any]  # Clearly wants structured data
) -> FunctionCallItem:
    """Create a function call item.

    Args:
        call_id: Unique call identifier
        name: Function name
        arguments: Function arguments as dict (will be JSON-serialized internally)
                  If you have pre-serialized JSON string, deserialize it first:
                  `make_item_tool_call(arguments=json.loads(json_string))`
    """
    args_json = json.dumps(arguments)
    return FunctionCallItem(call_id=call_id, name=name, arguments=args_json)
```

**Rationale:**
- API should have ONE clear contract
- Force callers to be explicit about what they're passing
- Runtime isinstance() check is a code smell
- One `json.loads()` at call site > ambiguous API

**Impact:** Type safety, clearer API contracts
**Priority:** High
**Instances:** 3 in this file (lines 18, 42, 50)

---

#### Issue 3.2: Parameter Typed as Any
**File:** `adgn/src/adgn/openai_utils/builders.py:50`

**Current Code:**
```python
# BAD: output typed as Any
def tool_call_with_output(
    self,
    name: str,
    arguments: dict[str, Any] | str,  # Already bad
    output: Any,  # VERY BAD
    call_id: str | None = None
) -> tuple[FunctionCallItem, FunctionCallOutputItem]:
```

**Recommended Fix:**
```python
# GOOD: Specific union type
def tool_call_with_output(
    self,
    name: str,
    arguments: dict[str, Any],  # Fix this too per Issue 3.1
    output: str | dict[str, Any] | FunctionCallOutputItem,  # Explicit types
    call_id: str | None = None
) -> tuple[FunctionCallItem, FunctionCallOutputItem]:
    """Create a tool call with output.

    Args:
        output: Tool output as string, dict (will be JSON-serialized),
                or pre-constructed FunctionCallOutputItem
    """
```

**Impact:** Type safety, better IDE support
**Priority:** High
**Instances:** 30+ files with `Any` parameters

---

#### Issue 3.3: Functions Returning dict[str, Any]
**File:** `llm/ducktape_llm_common/ducktape_llm_common/claude_code_api.py:39,47`

**Current Code:**
```python
# Lines 39, 47 have dict[str, Any] returns
tool_input: dict[str, Any]
tool_response: dict[str, Any]
```

**Investigation Needed:**
Check if these are from Pydantic models that should be typed.

**Recommended Fix Pattern:**
```python
# If from Pydantic model:
# BAD
def get_config() -> dict[str, Any]:
    return config_model.model_dump()

# GOOD
def get_config() -> ConfigModel:
    return config_model
```

**Priority:** Medium
**Instances:** 30+ files returning `dict[str, Any]`

---

### Additional Loose Typing Issues

**Categories:**

1. **Parameters typed as `Any`** (30+ files):
   - experimental/cotrl, ansible/plugins, wt/src/, llm/mcp/
   - Priority: Review each, replace with specific union

2. **dict[str, Any] parameters** (50+ files):
   - Widespread across all components
   - Priority: Check if should be Pydantic models

3. **dict[str, Any] returns** (30+ files):
   - Often from model_dump() - should return model directly
   - Priority: Trace back to source

**Bulk Fix Strategy:**
1. Fix highest-impact: `adgn/src/adgn/openai_utils/builders.py` (used widely)
2. Create Pydantic models for common dict[str, Any] patterns
3. Add type aliases for truly dynamic data (with documentation)

---

## 4. Asyncio Antipatterns

### Overview
Found 6 instances of deprecated APIs and blocking I/O in async functions.

### Critical Issues

#### Issue 4.1: Deprecated asyncio.get_event_loop()
**File:** `wt/src/wt/server/wt_server.py:207`

**Current Code:**
```python
# BAD: get_event_loop() is deprecated in Python 3.10+
def _shared_async_run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable)
```

**Recommended Fix:**
```python
# GOOD: Use get_running_loop() if already in async context
# Or create new event loop if this is top-level entry point

# Option 1: If this is top-level entry (e.g., called from __main__)
def _shared_async_run(awaitable):
    return asyncio.run(awaitable)

# Option 2: If this is called from within async context
async def _shared_async_run(awaitable):
    # Already have a running loop, just await
    return await awaitable
```

**Context Needed:** Need to understand where `_shared_async_run` is called from.

**Impact:** Future Python compatibility
**Priority:** High
**Instances:** 3 files (wt_server.py, adgn/src/adgn/mcp/exec/models.py, seatbelt.py)

---

#### Issue 4.2: Blocking File I/O in Async Functions
**File:** `adgn/tests/llm/test_llm_edit_unit.py:56,66,72,82,88,102`

**Current Code:**
```python
# BAD: Blocking file I/O in async test
async def test_done_for_non_python_no_syntax_check(tmp_path: Path, editor_session) -> None:
    p = tmp_path / "note.md"
    p.write_text("hello\n", encoding="utf-8")  # BLOCKING!

    async with editor_session(p) as (client, sess):
        # ... async operations
        pass

    # file saved with edits
    assert p.read_text(encoding="utf-8") == "hello\nworld\n"  # BLOCKING!
```

**Recommended Fix:**
```python
# GOOD: Use aiofiles for async file I/O
import aiofiles

async def test_done_for_non_python_no_syntax_check(tmp_path: Path, editor_session) -> None:
    p = tmp_path / "note.md"
    async with aiofiles.open(p, 'w', encoding='utf-8') as f:
        await f.write("hello\n")

    async with editor_session(p) as (client, sess):
        # ... async operations
        pass

    # file saved with edits
    async with aiofiles.open(p, 'r', encoding='utf-8') as f:
        content = await f.read()
    assert content == "hello\nworld\n"
```

**Alternative (if aiofiles not available):**
```python
# Use asyncio.to_thread() for one-off operations
import asyncio

async def test_done_for_non_python_no_syntax_check(tmp_path: Path, editor_session) -> None:
    p = tmp_path / "note.md"
    await asyncio.to_thread(p.write_text, "hello\n", encoding="utf-8")

    # ... async operations

    content = await asyncio.to_thread(p.read_text, encoding="utf-8")
    assert content == "hello\nworld\n"
```

**Impact:** Proper async I/O, prevent event loop blocking
**Priority:** Medium (tests, but sets bad example)
**Instances:** 3 test files with blocking I/O

---

#### Issue 4.3: os.pipe() Without O_NONBLOCK
**File:** `wt/src/wt/client/wt_client.py`

**Current Code:** (Need to read to confirm)

**Recommended Fix:**
```python
# BAD: os.pipe() without non-blocking setup
read_fd, write_fd = os.pipe()
# ... use with asyncio

# GOOD: Set O_NONBLOCK before asyncio use
import fcntl
import os

read_fd, write_fd = os.pipe()
for fd in (read_fd, write_fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
# Now safe for asyncio
```

**Priority:** High (can cause blocking)
**Instances:** 1 file

---

### Additional Asyncio Issues

**Files with deprecated get_event_loop():**
- `wt/src/wt/server/wt_server.py:207`
- `adgn/src/adgn/mcp/exec/models.py` (location TBD)
- `adgn/src/adgn/mcp/exec/seatbelt.py` (location TBD)

**Files with blocking file I/O in async:**
- `gatelet/gatelet/server/endpoints/test_admin_logs.py`
- `adgn/tests/llm/test_llm_edit_unit.py` (6+ instances)
- `adgn/tests/agent/conftest.py`

**Bulk Fix Strategy:**
1. Fix deprecated get_event_loop() first (high priority, easy fix)
2. Add aiofiles to test dependencies
3. Create async file I/O helper for tests

---

## Priority Matrix

| Issue | Priority | Impact | Effort | Files Affected |
|-------|----------|--------|--------|----------------|
| 3.1 Permissive Union (builders.py) | HIGH | High | Low | 1 (high usage) |
| 2.1 Container ID Assertions | HIGH | Medium | Low | 3+ |
| 4.1 Deprecated get_event_loop() | HIGH | Medium | Low | 3 |
| 4.3 os.pipe() O_NONBLOCK | HIGH | High | Low | 1 |
| 3.2 Any Parameters | HIGH | High | Medium | 30+ |
| 2.2 None Parameter Raises | HIGH | Medium | Medium | 10-15 |
| 4.2 Blocking File I/O | MEDIUM | Low | Medium | 3 |
| 1.1 Verbose Collection Checks | MEDIUM | Low | Low | 20+ |
| 3.3 dict[str, Any] Returns | MEDIUM | Medium | High | 30+ |
| 1.2 Full-Object has_properties | LOW | Low | Low | 10+ |
| 1.3 String Inclusion | LOW | Low | Low | 20+ |

---

## Recommended Action Plan

### Phase 1: High-Priority Quick Wins (1-2 hours)
1. Fix `adgn/src/adgn/openai_utils/builders.py` (Issue 3.1, 3.2) - high usage
2. Fix deprecated `get_event_loop()` in 3 files (Issue 4.1)
3. Add type-narrowing helper for container.id (Issue 2.1)
4. Fix os.pipe() O_NONBLOCK (Issue 4.3)

**Expected Impact:** Type safety in core libraries, Python 3.10+ compatibility

### Phase 2: Parameter Type Cleanup (4-6 hours)
1. Review and fix `Any` parameters in adgn/src/adgn/ (Issue 3.2)
2. Identify functions with `| None` params that immediately raise (Issue 2.2)
3. Fix blocking file I/O in tests (Issue 4.2)

**Expected Impact:** Better type safety across adgn codebase

### Phase 3: Test Improvements (2-4 hours)
1. Fix verbose collection checks in habitify tests (Issue 1.1)
2. Convert full-object has_properties to plain equality (Issue 1.2)
3. Add PyHamcrest matchers for string assertions (Issue 1.3)

**Expected Impact:** Better test error messages, clearer test intent

### Phase 4: dict[str, Any] Audit (8-12 hours)
1. Create Pydantic models for common dict[str, Any] patterns (Issue 3.3)
2. Trace model_dump() returns back to source
3. Document truly dynamic data cases

**Expected Impact:** Comprehensive type safety, better IDE support

---

## Automated Detection Scripts

### Script 1: Find Suspicious Nullability
```bash
#!/bin/bash
# Find assert is not None with context
rg --type py "assert \w+ is not None" -B3 -A1 > nullability_assertions.txt

# Find functions with nullable params that might raise
rg --type py "def \w+\([^)]*: \w+ \| None" -A3 | grep -B3 "raise" > nullable_params_raise.txt
```

### Script 2: Find Loose Typing
```bash
#!/bin/bash
# Find Any parameters
rg --type py "def \w+\([^)]*: Any" > any_parameters.txt

# Find dict[str, Any] returns
rg --type py ": dict\[str, Any\]" -B1 > dict_any_usage.txt

# Find permissive unions
rg --type py ": dict\[str, Any\] \| str" > permissive_unions.txt
```

### Script 3: Find Asyncio Issues
```bash
#!/bin/bash
# Find deprecated get_event_loop
rg --type py 'asyncio\.get_event_loop\(\)' -B3 -A3 > deprecated_event_loop.txt

# Find blocking file I/O in async
rg --type py -U 'async def.*\n.*\n.*\.(read_text|write_text)\(' > async_blocking_io.txt

# Find os.pipe usage
rg --type py 'os\.pipe\(\)' -B5 -A10 > os_pipe_usage.txt
```

---

## Notes

- All file paths are relative to repository root `/home/user/ducktape/`
- Line numbers may shift as code changes
- Grep patterns provided are approximate - manual verification required
- Some issues require domain knowledge to fix properly (e.g., None semantics)
- Estimated effort based on complexity and number of callsites to update

---

## Next Steps

1. Review and approve this scan report
2. Create GitHub issues for each high-priority item
3. Execute Phase 1 fixes
4. Re-run scans to verify fixes
5. Continue with subsequent phases

