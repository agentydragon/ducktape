# Priority Fixes from Code Quality Scan

Remaining actionable fixes from comprehensive code scan.

---

## 🟡 HIGH PRIORITY

### Fix 1: Parameter Typed as Any in High-Traffic Functions

Search strategy:
```bash
# Find all Any parameters
rg --type py "def \w+\([^)]*: Any" adgn/src/ -n

# Review each, determine actual types from:
# 1. isinstance() checks in function body
# 2. How parameter is used
# 3. What callers actually pass
```

**Common Pattern:**
```python
# BAD
def process(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data)

# GOOD
def process(data: dict[str, Any] | str) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data)
```

**Action Items:**
1. Review `adgn/src/adgn/openai_utils/` for Any parameters
2. Review `adgn/src/adgn/rspcache/` for Any parameters
3. Review `llm/ducktape_llm_common/` for Any parameters

**Priority:** Medium
**Estimated Instances:** 30+ files

---

### Fix 2: Functions Returning dict[str, Any]

**Pattern:**
```python
# If from Pydantic model:
# BAD
def get_config() -> dict[str, Any]:
    return config_model.model_dump()

# GOOD
def get_config() -> ConfigModel:
    return config_model
```

**Files with dict[str, Any] returns:** 30+ across all components

**Priority:** Medium
**Instances:** 30+ files returning `dict[str, Any]`

---

## 🟢 LOWER PRIORITY

### Fix 3: Remaining Verbose Test Assertions

**Pattern (20+ instances in other test files):**
```python
# BAD: Three assertions for "non-empty list of X"
assert_that(result, instance_of(list))
assert_that(result, only_contains(instance_of(X)))
assert len(result) > 0

# GOOD: Single composed assertion
from hamcrest import all_of, has_length, greater_than

assert_that(result, all_of(
    has_length(greater_than(0)),
    only_contains(instance_of(X))
))
```

**Files remaining:**
- `difftree/tests/test_*.py` (8 files)
- `claude/claude_optimizer/tests/test_*.py` (6 files)
- `wt/tests/*/test_*.py` (15+ files)

**Priority:** Low (test clarity, no functional impact)
**Effort:** ~1-2 hours for bulk fixes

---

## 📊 Progress Summary

**Remaining:**
- 2 Medium Priority (Any parameters, dict[str, Any] returns)
- 1 Low Priority (remaining verbose test assertions)

**Next Steps:**
1. Fix `Any` parameters in high-traffic modules (~2-4 hours)
2. Audit and fix `dict[str, Any]` returns (~4-8 hours)
3. Bulk fix remaining test assertions (~1-2 hours)

---

## 🔧 AUTOMATED DETECTION SCRIPTS

### Script: Find Any Parameters
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

### Script: Find dict[str, Any] Returns
```bash
#!/bin/bash
echo "=== Functions returning dict[str, Any] ==="
rg --type py ": dict\[str, Any\]" -B1

echo ""
echo "Check if should return Pydantic model instead"
```
