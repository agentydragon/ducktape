# Mypy Final[str] Type Inference Investigation

This directory contains a test scaffold to investigate an apparent bug in mypy 1.18+ where
`Final[str]` constants lose their type information when imported across modules, causing
`warn_return_any` to trigger false positives.

## Problem Statement

When using `warn_return_any = true` in mypy 1.18+, functions that return `Final[str]` constants
imported from other modules incorrectly trigger `no-any-return` errors, even though:

1. The constants are explicitly typed as `Final[str]`
2. Pyright correctly infers the types
3. The code is obviously correct

## Test Case

The `test_case/` directory contains a minimal reproduction:

- `constants.py`: Defines `Final[str]` constants
- `uris.py`: Functions that return these constants (directly or via `.format()`)
- `docker_env.py`: Property that returns a constant

Expected: All type checkers should infer `str` return types
Actual (mypy 1.18+): Mypy infers `Any`, triggering `no-any-return` errors

## Running Tests

### Test with various type checkers and mypy versions:
```bash
chmod +x test_typecheckers.sh
./test_typecheckers.sh
```

This will test with:
- Pyright (if installed)
- Mypy versions: 1.14.0, 1.15.0, 1.16.0, 1.17.0, 1.18.1, 1.18.2
- Both with and without `disable_expression_cache`

### Test pre-commit isolation:
```bash
chmod +x test_precommit_isolation.sh
./test_precommit_isolation.sh
```

This tests if pre-commit's isolation affects the issue.

## Expected Results

- **Pyright**: Should pass (no errors)
- **Mypy < 1.18**: TBD (need to test)
- **Mypy 1.18+**: May fail with `no-any-return` errors
- **Mypy 1.18+ with `disable_expression_cache`**: TBD

## Current Workarounds in ducktape

1. Added `--disable-expression-cache` flag to `.pre-commit-config.yaml`
2. Added `disable_expression_cache = true` to `adgn/pyproject.toml`
3. Added explicit type annotations: `uri: str = CONSTANT` before returning

## TODO

- [ ] Bisect mypy versions to find when this was introduced
- [ ] Test if `disable_expression_cache` actually helps
- [ ] Determine if pyright can replace mypy
- [ ] File upstream bug report with minimal reproduction
- [ ] Remove workarounds once fixed

## Related Links

- PR that introduced expression cache: https://github.com/python/mypy/pull/19505
- Mypy 1.18.1 release notes mention potential regressions from type caching optimizations
