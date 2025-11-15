# Final Resolution: Type Annotations Were Unnecessary

## TL;DR

**The type annotations we added to `uris.py` and `docker_env.py` were COMPLETELY UNNECESSARY.**

These files have ZERO mypy errors, with or without the annotations. The `no-any-return` errors we're seeing are in DIFFERENT files that have legitimate issues with JSON/dict operations.

## Investigation Summary

### What We Thought Was Happening

We believed mypy 1.18's expression cache was losing type information for `Final[str]` constants across module imports, causing false positive `no-any-return` errors in:
- `adgn/src/adgn/mcp/_shared/uris.py`
- `adgn/src/adgn/props/docker_env.py`
- `adgn/src/adgn/mcp/approval_policy/server.py`

### What's Actually Happening

**These files have NO ERRORS:**
```bash
$ mypy --config-file=adgn/pyproject.toml adgn/src/adgn/mcp/_shared/uris.py adgn/src/adgn/props/docker_env.py
Success: no issues found in 2 source files
```

**The real errors are in DIFFERENT files:**
```
adgn/src/adgn/rspcache/models.py:135: error: Returning Any from function declared to return "ErrorPayload"  [no-any-return]
adgn/src/adgn/rspcache/events.py:50: error: Returning Any from function declared to return "ResponseStatusEvent | ..."  [no-any-return]
adgn/src/adgn/openai_utils/model.py:163: error: Returning Any from function declared to return "dict[str, Any]"  [no-any-return]
adgn/src/adgn/llm/sysrw/openai_typing.py:181-343: (multiple errors)
adgn/src/adgn/agent/handler.py:130: error: Returning Any from function declared to return "dict[str, Any]"  [no-any-return]
... (12 more errors in various files)
```

### Why The Confusion?

1. When we ran `pre-commit run mypy --all-files`, we saw `no-any-return` errors
2. We assumed they were in the files we were looking at (uris.py, docker_env.py)
3. We added workaround annotations that "fixed" nothing (because nothing was broken)
4. The errors persisted (because they're in other files)

### Test Results

**Minimal test case:** ✅ PASSES (all mypy versions 1.14-1.18.2)
**Realistic test (actual files isolated):** ✅ PASSES
**Full adgn package:** ✗ 17 `no-any-return` errors (but NOT in uris.py/docker_env.py!)

## Files with REAL Issues (17 errors total)

These files have legitimate `no-any-return` issues that need proper fixes:

1. **adgn/src/adgn/rspcache/models.py** (2 errors)
   - Returning dict values without type narrowing

2. **adgn/src/adgn/rspcache/events.py** (1 error)
   - Union return from dict parsing

3. **adgn/src/adgn/openai_utils/model.py** (1 error)
   - Dict comprehension returns Any

4. **adgn/src/adgn/llm/sysrw/openai_typing.py** (6 errors)
   - OpenAI API response parsing (legitimately returns Any)

5. **adgn/src/adgn/agent/handler.py** (1 error)
   - Dict manipulation

6. **adgn/src/adgn/inop/io/task_loader.py** (1 error)
   - JSON loading

7. **adgn/src/adgn/mcp/gitea_mirror/server.py** (1 error)
   - Generic type parameter

8. **adgn/src/adgn/llm/sysrw/run_eval.py** (2 errors)
   - Dict operations

9. **adgn/src/adgn/mcp/testing/typed_stubs.py** (1 error)
   - Generic return type

## Actions Needed

### Remove Unnecessary Annotations

These type annotations should be REMOVED (they do nothing):
- ✗ `adgn/src/adgn/mcp/_shared/uris.py` - all the `uri: str = X; return uri` patterns
- ✗ `adgn/src/adgn/props/docker_env.py` - the `name: str = X; return name` pattern
- ✗ `adgn/src/adgn/mcp/approval_policy/server.py` - the `content: str` annotations

### Fix Real Issues

For the 17 actual errors, options are:
1. **Add proper type narrowing** where possible
2. **Use `cast()`** where type narrowing isn't feasible
3. **Add `# type: ignore[no-any-return]`** for legitimately dynamic code (JSON parsing, etc.)
4. **Disable warn_return_any for specific files** that work with highly dynamic APIs

### Remove Configuration Workarounds

These can be REMOVED (they're based on false premise):
- ✗ `--disable-expression-cache` flag in `.pre-commit-config.yaml`
- ✗ `disable_expression_cache = true` in `adgn/pyproject.toml`
- ✗ Comments about "mypy 1.18 expression cache bug"

## Conclusion

**There is NO mypy 1.18 bug.**
**There is NO issue with `Final[str]` cross-module type inference.**
**The annotations we added were cargo-cult programming based on misdiagnosis.**

The real work is to properly fix the 17 legitimate `no-any-return` errors in files that actually do return `Any` values from dict/JSON operations.
