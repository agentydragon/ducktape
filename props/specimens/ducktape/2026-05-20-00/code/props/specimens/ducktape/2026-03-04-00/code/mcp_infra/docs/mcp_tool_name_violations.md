# MCP Tool Name Violations - Complete List

**Generated:** 2025-12-10
**Status:** Phase 1 & 1.5 Complete (21/21 servers migrated)
**Remaining Work:** Test infrastructure and production code still use string literals/constants instead of `server.tool.name`

---

## Overview

This document catalogs all remaining locations where MCP tool names are referenced via:

- String literals (`"tool_name"`)
- Constants (`TOOL_NAME_CONSTANT`)

Instead of the canonical pattern: `server.tool_name.name`

---

## Category 1: Test Infrastructure (`tests/support/steps.py`)

### Constants Defined

```python
EXEC_TEST_TOOL_NAME = "exec"      # Line 31
FAIL_TEST_TOOL_NAME = "fail"      # Line 33
```

### Step Classes Using Constants

**Lines using ECHO_TOOL_NAME constant:**

- Line 213: `EchoCall` - `make_mcp_tool_call(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, ...)`

**Lines using SUBMIT_RESULT_TOOL_NAME constant:**

- Line 238: `SubmitGradeCall` - `make_mcp_tool_call(GRADER_SUBMIT_MOUNT_PREFIX, SUBMIT_RESULT_TOOL_NAME, ...)`

**Lines using tool_name parameter (needs server instance):**

- Line 188: `UiToolCall` - `make_mcp_tool_call(UI_MOUNT_PREFIX, self.tool_name, ...)`
- Line 203: `SendMessageCall` - `make_mcp_tool_call(UI_MOUNT_PREFIX, self.tool_name, ...)`

### Step Classes That Need Server Instances

1. `ExecDockerCall` - needs `RuntimeServer` instance
2. `ExecEditorCall` - needs `EditorServer` instance
3. `EchoCall` - needs `EchoServer` instance
4. `UiToolCall` - needs `UiServer` instance
5. `SendMessageCall` - needs `UiServer` instance
6. `SubmitGradeCall` - needs grader submit server instance
7. `UpsertIssueCall` - needs critic submit server instance

### Impact

- ~8 step classes in `tests/support/steps.py` need refactoring to accept server instances
- ~15+ test files use these step classes
- This is **Phase 3** work (deferred after Phase 2 recipes provide server instances)

---

## Category 2: Production Code Tool Name Constants

### File: `mcp_infra/testing/simple_servers.py:22`

```python
ECHO_TOOL_NAME = "echo"
```

**Usage:** Test helper constant
**Should be:** Remove constant, use `EchoServer().echo_tool.name`
**Priority:** LOW (test infrastructure)

### File: `props/core/critic/critic.py:68`

```python
UPSERT_ISSUE_TOOL_NAME = "upsert_issue"
```

**Usage:** Used in structured run expectations
**Should be:** Access via critic submit server instance
**Priority:** HIGH (production code)

### File: `props/core/grader/grader.py:76`

```python
SUBMIT_RESULT_TOOL_NAME = "submit_result"
```

**Usage:** Used in structured run expectations
**Should be:** Access via grader submit server instance
**Priority:** HIGH (production code)

### File: `props/core/lint_issue.py:514`

```python
DOCKER_EXEC_TOOL_NAME = "exec"
```

**Usage:** Legacy constant for docker exec tool
**Should be:** Use `runtime_server.exec_tool.name`
**Priority:** HIGH (production code)

---

## Category 3: Direct `call_tool()` with String Literals

### Production Code

**No production code violations** - all production code uses `build_mcp_function()` helper.

### Test Code (10 occurrences - acceptable)

Tests using direct FastMCP client `.call_tool()` with string literals:

- `tests/mcp/sandboxed_jupyter/test_*.py` (2 files)
- `tests/mcp/enhanced/flat_model/test_decorator.py` (4 calls)
- `tests/mcp/approval_policy/test_policy_validation.py` (3 calls)
- `tests/mcp/test_pg_middleware.py` (1 call)

**Priority:** LOW (test code, acceptable pattern for direct client usage)

---

## Category 4: `build_mcp_function()` Usage (65 occurrences)

### Production Code Uses (Acceptable)

These usages are **acceptable** because no server instance is available:

**Policy Evaluation (`agent_server/src/agent_server/policies/default_policy.py`):**

```python
UI_SEND = build_mcp_function(UI_MOUNT_PREFIX, "send_message")
UI_END = build_mcp_function(UI_MOUNT_PREFIX, "end_turn")
```

**Context:** Policy runs in Docker without server instances
**Status:** ✅ ACCEPTABLE

**Bootstrap Helpers (`agent_core/src/agent_core/bootstrap.py`):**

```python
tool_key = build_mcp_function(server, tool)
```

**Context:** Building synthetic tool calls before server introspection
**Status:** ✅ ACCEPTABLE (but could be improved in future)

**Compositor Clients (`mcp_infra/src/mcp_infra/compositor/clients.py`):**

```python
self._attach_name = build_mcp_function(COMPOSITOR_ADMIN_SERVER_NAME, "attach_server")
self._detach_name = build_mcp_function(COMPOSITOR_ADMIN_SERVER_NAME, "detach_server")
```

**Context:** Client-side tool name construction
**Status:** ✅ ACCEPTABLE

**UI Reducer (`agent_server/src/agent_server/server/reducer.py`):**

```python
build_mcp_function(UI_MOUNT_PREFIX, WellKnownTools.SEND_MESSAGE)
build_mcp_function(UI_MOUNT_PREFIX, WellKnownTools.END_TURN)
```

**Context:** UI state management, checking tool names
**Status:** ⚠️ COULD BE IMPROVED (use server instance when available)

### Production Code Uses (Should Fix)

**File: `props/core/lint_issue.py:261,376,461`**

```python
docker_tool_name = build_mcp_function(DOCKER_SERVER_NAME, DOCKER_EXEC_TOOL_NAME)
submit_tool_name = build_mcp_function("lint_submit", "submit_result")
```

**Should be:** Use server instances from recipe
**Priority:** MEDIUM (will be fixed by Phase 2 recipes)

**File: `props/core/grader/grader.py:530`**

```python
build_mcp_function(GRADER_SUBMIT_MOUNT_PREFIX, "submit_result")
```

**Should be:** Use server instance from recipe
**Priority:** MEDIUM (will be fixed by Phase 2 recipes)

### Test Code Uses (~40 occurrences)

**Status:** ✅ ACCEPTABLE - Tests use `build_mcp_function()` for expectations and assertions

---

## Summary by Priority

### 🔴 HIGH PRIORITY (Must Fix for Phase 2 DoD)

1. `props/core/critic/critic.py:68` - UPSERT_ISSUE_TOOL_NAME constant
2. `props/core/grader/grader.py:76` - SUBMIT_RESULT_TOOL_NAME constant
3. `props/core/lint_issue.py:514` - DOCKER_EXEC_TOOL_NAME constant

**Fix:** Remove constants, access via server instances from recipes

### 🟡 MEDIUM PRIORITY (Will be Fixed by Phase 2 Recipes)

4. `props/core/lint_issue.py:261,376,461` - `build_mcp_function()` calls
5. `props/core/grader/grader.py:530` - `build_mcp_function()` call

**Fix:** Recipes will provide server instances, eliminating need for name construction

### 🟢 LOW PRIORITY (Phase 3 or Deferred)

6. `tests/support/steps.py` - 8 step classes using constants (Phase 3 work)
7. `tests/support/test_mcp_factory_methods.py` - EXEC_TEST_TOOL_NAME usage
8. `tests/agent/test_tool_error_sequence.py` - FAIL_TEST_TOOL_NAME usage
9. `mcp_infra/testing/simple_servers.py:22` - ECHO_TOOL_NAME (test helper)
10. Direct `call_tool()` in tests (~10 occurrences) - acceptable pattern

### ✅ ACCEPTABLE (No Changes Needed)

- `build_mcp_function()` in policy evaluation (Docker isolation)
- `build_mcp_function()` in bootstrap helpers (pre-introspection)
- `build_mcp_function()` in test expectations/assertions
- Direct `call_tool()` in tests (FastMCP client pattern)

---

## Validation Commands

```bash
# Find tool name constants
rg '[A-Z_]+_TOOL_NAME\s*=' --type py -n

# Find make_mcp_tool_call with constants (not .name)
rg 'make_mcp_tool_call\([^,]+,\s*[A-Z_]+' --type py -n | grep -v '\.name'

# Find direct call_tool with string literals
rg 'call_tool\("[a-z_]+"' --type py -n

# Find build_mcp_function usage
rg 'build_mcp_function\(' --type py -l

# Check for any remaining string literal tool calls
rg 'make_mcp_tool_call.*"[a-z_]+"' src/ --type py -n
```

---

## Notes

**Why `build_mcp_function()` is sometimes acceptable:**

- Policy evaluation runs in Docker without server instances
- Bootstrap creates synthetic calls before server introspection
- Test expectations need to match against full namespaced names

**When to use `server.tool.name` vs `build_mcp_function()`:**

- ✅ Use `server.tool.name` when you have a server instance
- ⚠️ Use `build_mcp_function()` only when:
  - No server instance is available (policy Docker)
  - Building expectations/assertions in tests
  - Client-side tool name matching

**Phase 3 Impact:**

- Phase 2 recipes will provide server instances to tests
- This makes Phase 3 (test infrastructure migration) trivial
- Step classes just need to accept server parameters
