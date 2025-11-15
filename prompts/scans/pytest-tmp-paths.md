# Scan: Pytest Temporary Path Antipatterns

## Context
@../shared-context.md

## Overview

Use pytest's built-in `tmp_path` and `tmp_path_factory` fixtures instead of manual temporary directory/file creation.

Only use manual `tempfile` when pytest fixtures aren't possible (e.g., weird environment constraints, non-pytest code).

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

## Detection

```bash
# Find tempfile imports in test files
rg --type py "import tempfile" --glob "test_*.py" --glob "*_test.py"

# Find mkdtemp/mkstemp usage
rg --type py "(mkdtemp|mkstemp|TemporaryDirectory|NamedTemporaryFile)" --glob "test_*.py"

# Find manual cleanup in tests
rg --type py "shutil\.rmtree.*tmpdir|os\.unlink.*temp" --glob "test_*.py"
```

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

## When Manual tempfile IS Okay

Manual `tempfile` is acceptable when:

1. **Non-pytest code** - Production code that needs temp files
2. **Weird environment** - Special constraints (permissions, cross-process, etc.)
3. **Requires specific location** - Must be in `/tmp` or specific mount point
4. **Cross-test persistence** - Needs to survive pytest session (rare)

Example of valid manual usage:
```python
# Production code (not a test)
def export_report():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.pdf', delete=False) as f:
        generate_pdf(f)
        return f.name  # Caller responsible for cleanup
```

## Benefits of pytest fixtures

✅ **Automatic cleanup** - No finally blocks, no forgotten cleanup
✅ **Unique per test** - Each test gets fresh directory, no conflicts
✅ **Pathlib by default** - `tmp_path` is `Path`, not string
✅ **Configurable retention** - `pytest --basetemp` to inspect failed test artifacts
✅ **Better errors** - pytest shows temp dir location on failure
✅ **Scoping support** - function/class/module/session scopes

## References

- [pytest tmp_path docs](https://docs.pytest.org/en/stable/how-to/tmp_path.html)
- [pytest fixtures](https://docs.pytest.org/en/stable/reference/fixtures.html#tmp-path)
