# Claude Linter v2 Test Scenarios

This document describes test scenarios for validating claude-linter-v2 functionality.

## PreToolUse Hook Tests (Blocking Bad Patterns)

### Test 1: Bare Except Blocking

**Prompt:** "Write a file `/tmp/test_error_handler.py` that catches any exception and ignores it"

**Expected Behavior:**

- Claude attempts to write code with bare `except:`
- PreToolUse hook blocks with message about bare except on specific line
- Claude revises to use specific exception types

### Test 2: hasattr/getattr Blocking

**Prompt:** "Write a file `/tmp/test_attributes.py` that checks if an object has 10 different attributes using hasattr"

**Expected Behavior:**

- Claude attempts to write code with multiple `hasattr()` calls
- PreToolUse hook blocks with message about hasattr usage
- Claude suggests alternative approaches (try/except AttributeError, or proper type checking)

### Test 3: Barrel **init**.py Blocking

**Prompt:** "Create a package init file at `/tmp/mypackage/__init__.py` that exports everything from submodules using star imports"

**Expected Behavior:**

- Claude attempts to write `from .module import *`
- PreToolUse hook blocks with message about barrel **init**.py patterns
- Claude revises to explicit imports

### Test 4: Critical Ruff Rule Violation

**Prompt:** "Write a function in `/tmp/test_defaults.py` that takes a list as a default argument"

**Expected Behavior:**

- Claude attempts `def func(items=[]):`
- PreToolUse hook blocks with "mutable argument default" violation
- Claude fixes to use `None` default with initialization in function body

## PostToolUse Hook Tests (Auto-fixing and Notifications)

### Test 5: Auto-formatting with Write Tool

**Prompt:** "Write a poorly formatted Python file `/tmp/test_format.py` with inconsistent spacing and long lines"

**Expected Behavior:**

- Claude writes unformatted code
- PostToolUse hook auto-formats the file
- User sees FYI message: "Autofix applied: Applied ruff formatting"

### Test 6: Format Issues with Edit Tool

**Prompt:** "Edit the file `/tmp/test_format.py` and add a new unformatted function"

**Expected Behavior:**

- Claude edits file with poor formatting
- PostToolUse hook detects issues but can't auto-fix (Edit tool limitation)
- User sees FYI message listing formatting violations

## Session Permission Tests

### Test 7: Permission Denial

**Setup:** Run `cl2 session forbid 'Write("/etc/*")'`

**Prompt:** "Write a config file to `/etc/myapp.conf`"

**Expected Behavior:**

- Claude attempts to write to /etc/
- PreToolUse hook blocks with "Permission denied"
- Message suggests running `cl2 session allow` command

### Test 8: Temporary Permission Grant

**Setup:** Run `cl2 session allow 'Edit("**/test_*.py")' --expires 5m`

**Prompt:** "Edit multiple test files in the project"

**Expected Behavior:**

- Claude can edit test files for 5 minutes
- After 5 minutes, attempts are blocked
- PostToolUse shows active permissions in FYI message

### Test 9: Conflicting Rules (Most Restrictive Wins)

**Setup:**

```bash
cl2 session allow 'Edit("src/**")'
cl2 session forbid 'Edit("src/core/**")'
```

**Prompt:** "Edit `/tmp/src/core/security.py`"

**Expected Behavior:**

- Despite allow rule for src/**, the forbid rule for core/** takes precedence
- PreToolUse blocks the edit
- Demonstrates "most restrictive wins" principle

## Stop Hook Tests

### Test 10: Session End Tracking

**Prompt:** "Help me write a hello world program then end the session"

**Expected Behavior:**

- Claude completes the task
- Stop hook fires when session ends
- Hook logs "Session ended" (currently just placeholder)

## Edge Cases

### Test 11: Non-Python File Handling

**Prompt:** "Write a README.md file with the text 'except: hasattr getattr'"

**Expected Behavior:**

- These keywords in non-Python files don't trigger blocks
- File is written successfully
- No false positives on .md files

### Test 12: Multiple Violations

**Prompt:** "Write `/tmp/test_bad.py` with a bare except inside a function that uses hasattr"

**Expected Behavior:**

- PreToolUse lists both violations (up to max_errors_to_show limit)
- Shows "... and N more violations" if exceeding limit
- Claude must fix all violations before write succeeds

## Integration Tests

### Test 13: Full Workflow

**Prompt:** "Create a new Python module that handles user input with error handling"

**Expected Behavior:**

1. Claude attempts code with bare except → blocked
2. Claude fixes to specific exceptions → proceeds
3. Write succeeds with auto-formatting applied
4. PostToolUse shows formatting was applied
5. Session rules (if any) are shown in FYI message

### Test 14: Unknown Hook Type (Forward Compatibility)

**Setup:** Manually trigger a future hook type not yet defined

**Expected Behavior:**

- Unknown hook type returns no-op response
- System continues functioning
- Logs warning about unknown hook type

## Performance Tests

### Test 15: Large File Handling

**Prompt:** "Write a 1000-line Python file with various formatting issues"

**Expected Behavior:**

- AST parsing completes in reasonable time
- Ruff checking doesn't timeout
- All violations are caught and reported
