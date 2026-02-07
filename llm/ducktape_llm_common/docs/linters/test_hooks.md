# Claude Code Linter Hooks Test Procedure

This document provides test cases to verify the Claude Code linter hooks are working correctly.

## Prerequisites

1. Install the ducktape_llm_common package:

   ```bash
   pip install -e /path/to/ducktape_llm_common
   ```

2. Verify hooks are configured in `~/.claude/settings.json`:

   ```json
   "hooks": {
     "PreToolUse": [
       {
         "matcher": "Write",
         "hooks": [
           {
             "type": "command",
             "command": "claude-linter-pre-hook",
             "continue": true
           }
         ]
       }
     ],
     "PostToolUse": [
       {
         "matcher": "Write",
         "hooks": [
           {
             "type": "command",
             "command": "claude-linter-post-hook",
             "continue": true
           }
         ]
       }
     ]
   }
   ```

## Test Cases

### Test 1: Auto-fixable Violations Only

**File**: `test_autofix.py`

```python
from typing import Union, Optional

def process_data(value: Union[str, int]) -> Optional[str]:
    if value:
        return str(value)
    else:  # RET505 - superfluous else
        return None
```

**Expected Result**:

- Pre-hook: Should pass (all violations are fixable)
- Post-hook: Should auto-fix and report:
  - `UP007`: Convert `Union[str, int]` to `str | int`
  - `RET505`: Remove superfluous else
  - `RUF013`: Convert `Optional[str]` to `str | None`

### Test 2: Non-fixable Ruff Violations

**File**: `test_nonfixable.py`

```python
import requests

def fetch_data(url):
    response = requests.get(url)  # S113 - missing timeout
    return response.text
```

**Expected Result**:

- Pre-hook: Should block with error message about S113 violation
- File should NOT be created

### Test 3: Manual Pattern Violations (hasattr)

**File**: `test_hasattr.py`

```python
def check_attr(obj):
    if hasattr(obj, 'foo'):
        return obj.foo
    return None
```

**Expected Result**:

- Currently: Original `claude-linter-hook` blocks this
- TODO: Pre-hook should also check manual patterns

### Test 4: Mixed Violations

**File**: `test_mixed.py`

```python
from typing import Union
import requests

def process(value: Union[str, int]):  # UP007 - fixable
    if hasattr(value, 'upper'):  # hasattr - not fixable
        return value.upper()

def fetch(url):
    return requests.get(url).text  # S113 - not fixable
```

**Expected Result**:

- Pre-hook: Should block due to non-fixable violations (hasattr, S113)
- File should NOT be created

### Test 5: Clean File

**File**: `test_clean.py`

```python
def add(a: int, b: int) -> int:
    return a + b
```

**Expected Result**:

- Pre-hook: Pass
- Post-hook: No changes needed
- File created successfully

## Verification Steps

1. Try to create each test file using Claude Code's Write tool
2. Observe the hook output messages
3. Check if files were created or blocked as expected
4. For auto-fixed files, verify the changes were applied

## Hook Behavior Summary

| Hook                          | Purpose                      | Exit Code            | Blocks Write          |
| ----------------------------- | ---------------------------- | -------------------- | --------------------- |
| claude-linter-pre-hook        | Check non-fixable violations | 2 with continue:true | Yes, but allows retry |
| claude-linter-post-hook       | Auto-fix violations          | 0                    | No                    |
| claude-linter-hook (original) | Block ALL violations         | 2                    | Yes                   |

## Known Limitations

1. Pre-hook currently only checks ruff violations, not manual patterns like hasattr
2. The original claude-linter-hook is still active and may interfere with the new hooks
3. Hooks only process Write operations (new files), not Edit/MultiEdit
