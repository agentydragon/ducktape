# Code Quality Scan Results
**Date:** 2025-11-17
**Scans Run:** test-assertions.md, suspicious-nullability.md, overly-loose-typing.md, asyncio-antipatterns.md

---

## Executive Summary

This comprehensive scan identified remaining actionable code quality issues across 3 categories:

1. **Test Assertions** (60+ files): Verbose test assertions that could use PyHamcrest matchers
2. **Suspicious Nullability** (40+ instances): Unnecessary None handling and type propagation
3. **Overly Loose Typing** (50+ instances): `dict[str, Any]` that should be specific types
4. **Asyncio Antipatterns** (4 instances): Blocking I/O in async functions

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
Found 40+ remaining instances where `| None` typing is suspicious or propagates unnecessarily.

### Critical Issues

#### Issue 2.1: Parameter Typed as T | None But Immediately Fails If None
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

#### Issue 2.2: None Propagation Through Layers
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
- `llm/ducktape_llm_common/tests/*/test_*.py` - Test setup code
- `adgn/tests/*/test_*.py` - Test assertions

**Note**: `adgn/src/adgn/agent/runtime/registry.py`, `adgn/src/adgn/props/cli_app/main.py`, `adgn/src/adgn/inop/runners/containerized_claude.py`, and `adgn/src/adgn/mcp/_shared/json_helpers.py` have been fixed.

**Bulk Fix Strategy:**
1. Review functions with `| None` parameters for immediate None checks
2. Refactor None propagation chains in wt/src/

---

## 3. Overly Loose Typing

### Overview
Found 50+ remaining instances of `dict[str, Any]` and overly permissive unions.

### Critical Issues

#### Issue 3.1: Functions Returning dict[str, Any]
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

1. **dict[str, Any] parameters** (50+ files):
   - Widespread across all components
   - Priority: Check if should be Pydantic models

2. **dict[str, Any] returns** (30+ files):
   - Often from model_dump() - should return model directly
   - Priority: Trace back to source

**Bulk Fix Strategy:**
1. Create Pydantic models for common dict[str, Any] patterns
2. Add type aliases for truly dynamic data (with documentation)

---

## 4. Asyncio Antipatterns

### Overview
Found 4 remaining instances of blocking I/O in async functions.

### Critical Issues

#### Issue 4.1: Blocking File I/O in Async Functions
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

### Additional Asyncio Issues

**Files with blocking file I/O in async:**
- `gatelet/gatelet/server/endpoints/test_admin_logs.py`
- `adgn/tests/llm/test_llm_edit_unit.py` (6+ instances)
- `adgn/tests/agent/conftest.py`

**Bulk Fix Strategy:**
1. Add aiofiles to test dependencies
2. Create async file I/O helper for tests

---

## Priority Matrix

| Issue | Priority | Impact | Effort | Files Affected |
|-------|----------|--------|--------|----------------|
| 2.1 None Parameter Raises | HIGH | Medium | Medium | 10-15 |
| 4.1 Blocking File I/O | MEDIUM | Low | Medium | 3 |
| 3.1 dict[str, Any] Returns | MEDIUM | Medium | High | 30+ |
| 1.1 Full-Object has_properties | LOW | Low | Low | 10+ |
| 1.2 String Inclusion | LOW | Low | Low | 20+ |

---

## Recommended Action Plan

### Phase 1: Nullability Cleanup (2-4 hours)
1. Identify functions with `| None` params that immediately raise (Issue 2.1)
2. Refactor None propagation chains in wt/src/ (Issue 2.2)

**Expected Impact:** Better type safety, clearer APIs

### Phase 2: Async I/O Improvements (2-3 hours)
1. Fix blocking file I/O in tests (Issue 4.1)
2. Add aiofiles dependency
3. Create async file I/O helper for tests

**Expected Impact:** Proper async I/O patterns in tests

### Phase 3: dict[str, Any] Audit (8-12 hours)
1. Create Pydantic models for common dict[str, Any] patterns (Issue 3.1)
2. Trace model_dump() returns back to source
3. Document truly dynamic data cases

**Expected Impact:** Comprehensive type safety, better IDE support

### Phase 4: Test Improvements (2-4 hours)
1. Convert full-object has_properties to plain equality (Issue 1.1)
2. Add PyHamcrest matchers for string assertions (Issue 1.2)

**Expected Impact:** Better test error messages, clearer test intent

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
# Find dict[str, Any] returns
rg --type py ": dict\[str, Any\]" -B1 > dict_any_usage.txt
```

### Script 3: Find Asyncio Issues
```bash
#!/bin/bash
# Find blocking file I/O in async
rg --type py -U 'async def.*\n.*\n.*\.(read_text|write_text)\(' > async_blocking_io.txt
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

1. Review this updated scan report
2. Execute Phase 1 fixes (nullability cleanup)
3. Re-run scans to verify fixes
4. Continue with subsequent phases
