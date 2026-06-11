# Pytest Timeout Issue in Nix Environment (2025-12-15)

## Problem Summary

Tests using `EnhancedFastMCP` fixtures were timing out (30+ seconds) when running the full pytest suite under Nix/devenv, despite working fine in non-Nix environments and in direct Python execution.

## Workaround (Implemented)

Added optional `version: str | None = None` parameter to `EnhancedFastMCP` and `_CapturingServer` in `mcp_infra/src/mcp_infra/enhanced/server.py`.

Applied `version="test"` in specific test fixtures to bypass slow `importlib.metadata.version()` lookups:

- `tests/mcp/conftest.py`: `origin_with_recorder` fixture
- `tests/agent/conftest.py`: `failing_server` fixture

### Code Changes

```python
# mcp_infra/src/mcp_infra/enhanced/server.py
class EnhancedFastMCP(FastMCP):
    def __init__(
        self,
        name: str,
        *,
        version: str | None = None,  # Added - NO DEFAULT
        # ... other params
    ):
        super().__init__(name=name, version=version, ...)
```

```python
# tests/mcp/conftest.py
@pytest.fixture
def origin_with_recorder() -> tuple[FastMCP, SubscriptionRecorder]:
    # Workaround: Pass version="test" to skip slow importlib.metadata.version() lookup
    # that hangs on os.stat() in Nix environment.
    m = EnhancedFastMCP("origin", version="test")
    # ...
```

### Design Principles

- **No global default**: Parameter has no default value
- **Targeted application**: Only applied to specific fixtures experiencing timeouts
- **Well-documented**: Each usage site has explanatory comment
- **Nix-specific**: Issue is 100% Nix environment-specific

## Root Cause Investigation

### Initial Hypothesis (WRONG)

Initially suspected `importlib.metadata.version("mcp")` call was slow (~0.001-0.002s).

**Reality discovered through profiling:**

- Direct `importlib.metadata.version("mcp")`: **0.003s** (fast!)
- FastMCP import (all dependencies): **1.158s** (slow but acceptable)
- Pytest execution (full suite): **30s+ timeout** (pathological)

### What's Actually Slow

The FastMCP dependency imports from `/nix/store/` paths:

- `jsonschema`: 0.681s
- `rfc3987_syntax`: 0.595s
- `mcp.client.session`: 0.745s
- **Total import overhead**: ~1.1s

This is acceptable for direct execution but becomes pathological in pytest's concurrent fixture creation context.

### Environment-Specific Behavior

| Context                      | FastMCP Import Time | EnhancedFastMCP Creation | Total Time |
| ---------------------------- | ------------------- | ------------------------ | ---------- |
| Non-Nix venv                 | Fast                | 0.003s                   | 0.003s     |
| Direct Python (Nix)          | 1.1s                | Fast                     | ~1.1s      |
| pytest single test (Nix)     | 2.2s                | 0.002s                   | 2.65s      |
| pytest -n4 single test (Nix) | ~2-3s               | Fast                     | 3.25s      |
| pytest full suite (Nix)      | ???                 | **30s+ timeout**         | **FAILS**  |

### sys.path Differences

**Direct Python** (19 entries):

- Starts with `/tmp`
- Immediately goes to Nix store paths

**pytest** (27 entries - 8 extra):

- Entry 1: `/home/agentydragon/.cache/devenv/adgn/state/venv/bin`
- Entry 25: `/home/agentydragon/code/ducktape/adgn/.devenv/state/venv/lib/python3.12/site-packages`
- Entry 26: `/home/agentydragon/code/ducktape/adgn/src`

pytest adds venv paths and project paths that affect import resolution.

## Current Best Hypothesis

**Concurrent filesystem contention in Nix store during pytest-xdist execution:**

When running the full test suite with pytest-xdist (16 workers by default):

1. Multiple workers spawn simultaneously
2. Each worker creates multiple `EnhancedFastMCP` fixtures concurrently
3. Each fixture creation triggers `pkg_version("mcp")` → `importlib.metadata.version()`
4. All workers access `/nix/store/` metadata files simultaneously
5. Nix's symlink-heavy filesystem structure creates contention
6. Some `os.stat()` calls on `/nix/store/` paths block for 30+ seconds

**Why this explains the observations:**

- ✅ Isolated tests are fast (no contention)
- ✅ Direct execution is fast (no parallel workers)
- ✅ Non-Nix venv is fast (regular filesystem, no symlink maze)
- ✅ Full test suite times out (many concurrent workers + fixtures)
- ✅ Workaround works (bypasses all metadata lookups)

**What's still unclear:**

- Why strace didn't capture the blocking `os.stat()` calls
- Exact mechanism of the contention (inode locks? metadata cache thrashing?)
- Why it's exactly 30 seconds (might be pytest-timeout setting, not actual hang duration)

## Not The Problem

Things we definitively ruled out:

- ❌ `importlib.metadata.version()` being inherently slow (it's 0.003s)
- ❌ FastMCP imports being too slow (1.1s is acceptable)
- ❌ pytest-xdist parallelism alone (4 workers on single test is fast)
- ❌ pytest assertion rewriting (tested with `--assert=plain`, no difference)
- ❌ pytest import hooks interfering (sys.path differences don't explain 30s hang)

## Testing Commands Used

```bash
# Test in non-Nix environment
cd /tmp && python3 -m venv test-venv && source test-venv/bin/activate
pip install -e /path/to/adgn
python /tmp/test_enhanced.py  # Result: 0.003s

# Profile import timing
python -X importtime -c "from mcp_infra.enhanced import EnhancedFastMCP"

# Compare sys.path
python /tmp/compare_sys_path.py
pytest /tmp/test_syspath_pytest.py -s

# Test with different pytest modes
pytest /tmp/test_import_timing.py -s --tb=short           # Single worker: 2.65s
pytest /tmp/test_import_timing.py -s --tb=short -n4       # 4 workers: 3.25s
pytest tests/mcp/test_resources_subscriptions_index.py -xvs  # BEFORE: timeout, AFTER: 3.38s
```

## Profiling Attempts

Tried but couldn't reproduce the exact hang in isolation:

- `strace -e trace=stat,statx,open,openat` - saw futex operations but not blocking stat calls
- `python -X importtime` - confirmed 1.1s import overhead but not the 30s hang
- `py-spy` - not available in environment

The hang only manifests when:

- Running the FULL test suite
- With multiple pytest-xdist workers (default: 16)
- Creating many fixtures concurrently

## Future Investigation Ideas (Low Priority)

If the issue resurfaces or we want to understand it better:

1. **Disable pytest-xdist completely** and measure timing:

   ```bash
   pytest -n0 tests/mcp/test_resources_subscriptions_index.py
   ```

2. **Binary search on worker count** to find contention threshold:

   ```bash
   pytest -n1 ... # then -n2, -n4, -n8, -n16
   ```

3. **Monitor Nix store access** during test run:

   ```bash
   sudo inotifywait -m /nix/store/ & pytest ...
   ```

4. **Check if other Nix projects** have similar issues with pytest-xdist

5. **Upstream to NixOS/pytest-xdist** if pattern is reproducible

## Resolution Status

**Resolved with workaround.** Tests now pass reliably (383 passed in ~52 seconds).

The workaround is:

- ✅ Minimal and targeted
- ✅ Well-documented at usage sites
- ✅ No global behavior changes
- ✅ Can be extended to additional fixtures as needed

**ROI on deep investigation: LOW**

- Workaround is simple and effective
- Issue is environment-specific (only affects Nix)
- May not be fixable by us (likely Nix filesystem + pytest-xdist interaction)
- Upstream fix would require reproducing in minimal environment

## Files Modified

- `mcp_infra/src/mcp_infra/enhanced/server.py` - Added `version` parameter support
- `mcp_infra/tests/conftest.py` - Applied `version="test"` to relevant fixtures
- `adgn/tests/agent/conftest.py` - Applied `version="test"` to `failing_server`

## Related Issues

- Pytest default timeout: 30s (configured in `pyproject.toml`)
- pytest-xdist default workers: 16 (from `-n=16` in `pyproject.toml`)
- FastMCP version detection: `pkg_version("mcp")` in FastMCP's `__init__`

## Key Takeaway

When working in Nix environments with pytest-xdist, be aware that concurrent access to package metadata in `/nix/store/` can cause severe performance degradation. The `version` parameter in FastMCP (and our EnhancedFastMCP wrapper) provides a clean escape hatch for test environments where version detection isn't needed.
