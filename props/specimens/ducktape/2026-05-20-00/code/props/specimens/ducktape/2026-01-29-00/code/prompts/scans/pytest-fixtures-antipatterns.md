# Scan: Pytest Antipatterns and Best Practices

## Context

@../shared-context.md

## Overview

Pytest provides powerful fixtures and utilities that should be used instead of manual environment manipulation. This scan identifies tests that manually manage:

- **Temporary files/directories** - Use `tmp_path` instead of `tempfile`
- **Environment variables** - Use `monkeypatch` instead of `os.environ` manipulation
- **Working directory** - Use `monkeypatch.chdir()` instead of `os.chdir()`
- **File system state** - Use `tmp_path` for test file operations
- **System attributes** - Use `monkeypatch.setattr()` instead of direct mutation

Manual manipulation is error-prone (forgotten cleanup, test isolation failures, race conditions). Pytest fixtures provide automatic cleanup and isolation.

## Pattern: Manual tempfile Instead of Pytest Fixtures

### BAD: Manual tempfile usage

```python
import tempfile
import os

def test_file_operation():
    # BAD: Manual temp directory
    tmpdir = tempfile.mkdtemp()
    try:
        filepath = os.path.join(tmpdir, "test.txt")
        with open(filepath, "w") as f:
            f.write("test")
        # ... test logic ...
    finally:
        # Manual cleanup needed!
        import shutil
        shutil.rmtree(tmpdir)

def test_another():
    # BAD: Manual temp file
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, b"data")
        os.close(fd)
        # ... test logic ...
    finally:
        os.unlink(path)
```

### GOOD: Use pytest fixtures

```python
from pathlib import Path

def test_file_operation(tmp_path: Path):
    # ✓ GOOD: pytest provides tmp_path, auto-cleanup
    filepath = tmp_path / "test.txt"
    filepath.write_text("test")
    # ... test logic ...
    # No cleanup needed - pytest handles it!

def test_multiple_files(tmp_path: Path):
    # ✓ GOOD: Can create subdirectories
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "file.txt").write_text("data")

# For session/module scope
@pytest.fixture(scope="session")
def shared_data_dir(tmp_path_factory):
    # ✓ GOOD: tmp_path_factory for shared temp dirs
    return tmp_path_factory.mktemp("shared")
```

## Pattern 2: Manual Environment Variable Manipulation

### BAD: Direct os.environ manipulation

```python
import os

def test_env_variable():
    # BAD: Manual env var manipulation
    old_value = os.environ.get("API_KEY")
    try:
        os.environ["API_KEY"] = "test-key"
        # ... test logic ...
    finally:
        if old_value is None:
            del os.environ["API_KEY"]
        else:
            os.environ["API_KEY"] = old_value

def test_another():
    # BAD: Forgotten cleanup!
    os.environ["DEBUG"] = "true"
    # ... test logic ...
    # No cleanup - pollutes other tests!
```

### GOOD: Use monkeypatch fixture

```python
def test_env_variable(monkeypatch):
    # ✓ GOOD: monkeypatch auto-restores
    monkeypatch.setenv("API_KEY", "test-key")
    # ... test logic ...
    # Auto-restored after test!

def test_delete_env(monkeypatch):
    # ✓ GOOD: Can also delete env vars
    monkeypatch.delenv("OPTIONAL_VAR", raising=False)

def test_multiple_env(monkeypatch):
    # ✓ GOOD: Multiple env vars
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("DEBUG", "true")
```

## Pattern 3: Manual Working Directory Changes

### BAD: Direct os.chdir()

```python
import os

def test_working_directory():
    # BAD: Manual chdir with try/finally
    original_cwd = os.getcwd()
    try:
        os.chdir("/tmp")
        # ... test logic ...
    finally:
        os.chdir(original_cwd)

def test_another():
    # BAD: Forgotten restoration!
    os.chdir("/some/path")
    # ... test logic ...
    # No restoration - breaks other tests!
```

### GOOD: Use monkeypatch.chdir()

```python
def test_working_directory(monkeypatch, tmp_path):
    # ✓ GOOD: monkeypatch auto-restores cwd
    test_dir = tmp_path / "workdir"
    test_dir.mkdir()
    monkeypatch.chdir(test_dir)
    # ... test logic ...
    # Auto-restored after test!

def test_with_files(monkeypatch, tmp_path):
    # ✓ GOOD: Combine tmp_path + chdir
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    (work_dir / "config.txt").write_text("data")
    monkeypatch.chdir(work_dir)
    # Now relative paths work in work_dir
```

## Pattern 4: Manual Attribute/Module Patching

### BAD: Direct mutation with manual restore

```python
import sys

def test_module_attribute():
    # BAD: Manual attribute mutation
    original = sys.argv
    try:
        sys.argv = ["program", "--test"]
        # ... test logic ...
    finally:
        sys.argv = original

# BAD: Module-level mutation
MY_CONSTANT = 100

def test_constant():
    global MY_CONSTANT
    old = MY_CONSTANT
    try:
        MY_CONSTANT = 200
        # ... test logic ...
    finally:
        MY_CONSTANT = old
```

### GOOD: Use monkeypatch.setattr()

```python
import sys

def test_module_attribute(monkeypatch):
    # ✓ GOOD: monkeypatch auto-restores
    monkeypatch.setattr(sys, "argv", ["program", "--test"])
    # ... test logic ...
    # Auto-restored!

def test_constant(monkeypatch):
    # ✓ GOOD: Works for module attributes too
    import mymodule
    monkeypatch.setattr(mymodule, "MY_CONSTANT", 200)
    # ... test logic ...

def test_object_method(monkeypatch):
    # ✓ GOOD: Can patch methods
    def mock_method(self):
        return "mocked"
    monkeypatch.setattr(MyClass, "method", mock_method)
```

## Pattern 5: Manual File Operations in Test Directory

### BAD: Creating files in project directory

```python
import os

def test_file_processing():
    # BAD: Creates file in project directory!
    test_file = "test_data.txt"
    try:
        with open(test_file, "w") as f:
            f.write("data")
        # ... test logic ...
    finally:
        os.unlink(test_file)

def test_another():
    # BAD: Pollutes project directory
    os.mkdir("test_output")
    # ... test logic ...
    import shutil
    shutil.rmtree("test_output")
```

### GOOD: Use tmp_path for all test files

```python
def test_file_processing(tmp_path):
    # ✓ GOOD: All files in isolated tmp_path
    test_file = tmp_path / "test_data.txt"
    test_file.write_text("data")
    # ... test logic ...
    # Auto-cleaned!

def test_directory_structure(tmp_path):
    # ✓ GOOD: Create complex structures in tmp_path
    output_dir = tmp_path / "test_output"
    output_dir.mkdir()
    (output_dir / "results.json").write_text("{}")
    # Auto-cleaned!
```

## Detection Strategy

**MANDATORY Step 0**: Discover ALL pytest antipatterns in test files.

- This scan is **required** - do not skip this step
- You **must** read and process ALL test antipattern output using your intelligence
- High recall required, high precision NOT required - you determine which should use pytest fixtures
- Review each for: manual cleanup, test isolation risks, forgotten restoration, can use fixtures
- Prevents lazy analysis by forcing examination of ALL environment/file manipulation in tests

```bash
# 1. Find ALL os.* usage in test files (environment, file system, process manipulation)
rg --type py '\bos\.' --glob "test_*.py" --glob "*_test.py" -B 2 -A 2 --line-number

# 2. Find ALL tempfile usage in test files
rg --type py 'import tempfile|from tempfile import|tempfile\.' --glob "test_*.py" --glob "*_test.py" -B 1 -A 3 --line-number

# 3. Find ALL TemporaryDirectory, mkdtemp, mkstemp usage
rg --type py '(TemporaryDirectory|mkdtemp|mkstemp|NamedTemporaryFile)' --glob "test_*.py" --glob "*_test.py" -B 2 -A 2 --line-number

# 4. Find ALL manual cleanup patterns (try/finally in tests)
rg --type py '^[[:space:]]*try:' --glob "test_*.py" --glob "*_test.py" -A 10 --line-number

# 5. Find ALL os.environ manipulation
rg --type py 'os\.environ\[' --glob "test_*.py" --glob "*_test.py" -B 2 -A 2 --line-number

# 6. Find ALL os.chdir usage
rg --type py 'os\.chdir\(' --glob "test_*.py" --glob "*_test.py" -B 2 -A 2 --line-number

# 7. Find ALL sys.argv manipulation
rg --type py 'sys\.argv\s*=' --glob "test_*.py" --glob "*_test.py" -B 2 -A 2 --line-number

# 8. Find ALL global mutations in tests
rg --type py '^[[:space:]]*global \w+' --glob "test_*.py" --glob "*_test.py" -B 1 -A 3 --line-number

# 9. Find file creation in project directory (suspicious patterns)
rg --type py '(open\(|Path\()\s*["\'](?!/)' --glob "test_*.py" --glob "*_test.py" -B 2 -A 1 --line-number

# Count total antipattern candidates
echo "Total os.* usage:" && rg --type py '\bos\.' --glob "test_*.py" --glob "*_test.py" | wc -l
```

**What to review for each pattern:**

1. **os.environ usage**: Should use `monkeypatch.setenv()` / `monkeypatch.delenv()`
2. **os.chdir usage**: Should use `monkeypatch.chdir()`
3. **tempfile usage**: Should use `tmp_path` fixture (unless testing tempfile itself)
4. **os.unlink/rmtree**: Manual cleanup indicates should use `tmp_path`
5. **try/finally in tests**: Check if it's for cleanup that fixtures could handle
6. **sys.argv/module mutations**: Should use `monkeypatch.setattr()`
7. **File creation without tmp_path**: Files should be in `tmp_path`, not project dir
8. **global mutations**: Should use `monkeypatch.setattr()` on module

**Process ALL output**: Read each case, use your judgment to identify which should use pytest fixtures.

---

**Primary Method AFTER Step 0**: Manual code reading of test files to identify patterns.

**Why automation is insufficient**:

- Some `tempfile` usage might be intentional (testing tempfile handling itself)
- Need to understand test intent: does test actually need manual temp path creation?
- Some tests can't use pytest fixtures (e.g., testing subprocess that creates temp files)

**Discovery aids** (candidates for manual review):

```bash
# Find tempfile imports in test files (may be intentional)
rg --type py "import tempfile" --glob "test_*.py" --glob "*_test.py"

# Find mkdtemp/mkstemp usage (check if tmp_path would work)
rg --type py "(mkdtemp|mkstemp|TemporaryDirectory|NamedTemporaryFile)" --glob "test_*.py"

# Find manual cleanup in tests (strong signal for manual temp usage)
rg --type py "shutil\.rmtree.*tmpdir|os\.unlink.*temp" --glob "test_*.py"
```

**Manual review**: Check each case to determine if `tmp_path` fixture would work.

## Fix Strategy

### Replace mkdtemp → tmp_path

```python
# Before
import tempfile
tmpdir = tempfile.mkdtemp()

# After
def test_foo(tmp_path: Path):
    # tmp_path is already a directory
```

### Replace mkstemp → tmp_path

```python
# Before
fd, path = tempfile.mkstemp()
os.write(fd, b"data")
os.close(fd)

# After
def test_foo(tmp_path: Path):
    path = tmp_path / "tempfile"
    path.write_bytes(b"data")
```

### Replace TemporaryDirectory → tmp_path

```python
# Before
with tempfile.TemporaryDirectory() as tmpdir:
    # ...

# After
def test_foo(tmp_path: Path):
    # tmp_path is the directory
```

### Session-scoped temp directories

```python
# Before
_session_tmpdir = None

def setup_module():
    global _session_tmpdir
    _session_tmpdir = tempfile.mkdtemp()

def teardown_module():
    shutil.rmtree(_session_tmpdir)

# After
@pytest.fixture(scope="session")
def session_tmpdir(tmp_path_factory):
    return tmp_path_factory.mktemp("session")
```

## When Manual Manipulation IS Okay

Manual manipulation is acceptable when:

### 1. Non-pytest code

Production code can use `tempfile`, `os.environ`, etc.:

```python
# Production code (not a test)
def export_report():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.pdf', delete=False) as f:
        generate_pdf(f)
        return f.name  # Caller responsible for cleanup
```

### 2. Testing the mechanism itself

```python
def test_env_var_handling():
    # OK: Testing how code reacts to missing env vars
    import os
    # This is testing os.environ behavior itself
```

### 3. Weird environment constraints

- Special permissions requirements
- Cross-process communication
- Must be in specific location (`/tmp`, specific mount point)
- Cross-test persistence needed (very rare)

### 4. Setup/teardown in conftest.py

Session-level setup that pytest fixtures can't handle (also rare).

## Benefits of pytest fixtures

### tmp_path / tmp_path_factory

✅ **Automatic cleanup** - No finally blocks, no forgotten cleanup
✅ **Unique per test** - Each test gets fresh directory, no conflicts
✅ **Pathlib by default** - `tmp_path` is `Path`, not string
✅ **Configurable retention** - `pytest --basetemp` to inspect failed test artifacts
✅ **Better errors** - pytest shows temp dir location on failure
✅ **Scoping support** - function/class/module/session scopes

### monkeypatch

✅ **Automatic restoration** - All changes reverted after test
✅ **Test isolation** - Changes don't leak between tests
✅ **No try/finally** - Cleaner test code
✅ **Context tracking** - pytest tracks what was changed
✅ **Multiple changes** - Can patch many things, all restored
✅ **Deletion support** - Can delete env vars, restore original state

## References

- [pytest tmp_path docs](https://docs.pytest.org/en/stable/how-to/tmp_path.html)
- [pytest monkeypatch docs](https://docs.pytest.org/en/stable/how-to/monkeypatch.html)
- [pytest fixtures reference](https://docs.pytest.org/en/stable/reference/fixtures.html)
- [pytest best practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
