# Investigation Findings

## TL;DR

**The minimal test case passes in ALL mypy versions (1.14-1.18.2)**.
**This is NOT a mypy bug**. The issue is specific to the adgn codebase structure.

## Test Results

### Type Checkers Tested
- **Pyright 1.1.407**: ✓ PASSED (0 errors)
- **Mypy 1.14.0 - 1.18.2**: ✓ ALL PASSED
- **Pre-commit isolation**: ✓ PASSED

### Key Findings

1. **Simple `Final[str]` constants work perfectly**: When importing `Final[str]` constants from another module and returning them, mypy correctly infers the return type as `str`, not `Any`.

2. **`disable_expression_cache` is unnecessary**: This flag doesn't even exist in mypy < 1.18 (it shows as "Unrecognized option"). In 1.18+, it makes no difference for the simple test case.

3. **The problem is elsewhere**: The issues we're seeing in the actual adgn codebase must be caused by:
   - Complex package structure (namespace packages, nested imports)
   - Third-party library type stubs
   - Specific mypy configuration interactions
   - Or the type annotations we added ARE actually needed for some other reason

## What This Means

The workarounds we added may be:
1. **Addressing a different issue** than what we thought
2. **Unnecessary** if we can identify and fix the root cause
3. **Working around bad type stubs** in third-party libraries

## Next Steps

1. **Investigate adgn package structure**: What's different about the actual code that causes mypy to fail?

2. **Test with actual adgn files**: Copy the real constants.py and uris.py to see if they reproduce the issue

3. **Check third-party dependencies**: Maybe pydantic, fastapi, or fastmcp have type stub issues

4. **Remove workarounds**: If we can identify the real cause, remove the unnecessary type annotations

## Code Artifacts

- `run_tests.py`: Comprehensive test framework with adapters for mypy/pyright/pre-commit
- `test_case/`: Minimal reproduction that PASSES in all versions
- `results/test_results.json`: Detailed test results

## Conclusion

**We were solving the wrong problem**. The type annotations (`uri: str = CONSTANT`) we added are workarounds, but not for a mypy 1.18 expression cache bug. We need to dig deeper into the actual adgn code to understand what's really happening.
