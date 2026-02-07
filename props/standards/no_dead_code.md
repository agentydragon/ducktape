---
title: No dead code (incl. unreachable logic and test‑only prod code)
kind: outcome
---

There should be no dead production code: no unused symbols or unreachable branches live in production directories.
Prod code exercised only in tests is either relocated to explicit test helpers (preferred), or clearly marked as test-only.
If established invariants or type reasoning make a branch impossible, delete it.
Any formally-dead "should never happen" branches only contain an immediate `assert` or `TypeError`.

## Acceptance criteria (checklist)

- Unused symbols (functions, classes, variables, constants) are removed
- Unreachable branches (by invariants/types) are removed; if a "can't happen" guard is desired, keep at most an `assert` or `TypeError`.
- Switches/if‑chains do not include arms for states that cannot occur given the function’s contract
- Mutually exclusive guards and redundant checks are collapsed (no `if a and not a`, `if a: return; ... if a: ...`)
- Code only invoked from tests is clearly marked as such (`MakeTestFooObject`, `test_helpers.py`, ...)
- Feature‑flag or compatibility shims only allowed when actually referenced; stale flags/shims are removed once disabled across environments

## Negative examples

Do not include branches ruled out by type signatures:

```python
def get_user(uid: uuid.UUID) -> User:
    if not uid:
        return None  # impossible given function contract - delete
    return db.load_user(uid)
```

No checks that will always evaluate to `true` / `false` given execution state:

```go
if basePath == "" {
    full := matchesGlob(pattern, path)
    base := matchesGlob(pattern, filepath.Base(path))
    return full || base
}
...
if !validFile || basePath == "" {  // remove basePath check - ruled out above
    return false
}
```

Speculative fallback that cannot happen given upstream checks:

```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["fast", "slow"])  # upstream constraint
# ... later in code
if mode not in {"fast", "slow"}:
    # unreachable: CLI parser restricts choices; delete branch
    return default()
```

Default handling in match — wrong: provides behavior for an impossible case:

```python
match mode:
    case "fast":
        run_fast()
    case "slow":
        run_slow()
    case _:
        return default()  # unreachable; delete or turn into a hard guard
```

## Positive examples

Hard guard instead of fallback — acceptable sentinel:

```python
match mode:
    case "fast":
        run_fast()
    case "slow":
        run_slow()
    case _:
        raise AssertionError(f"unreachable mode: {mode!r}")
```

```python
# Or a simple membership assertion
assert mode in {"fast", "slow"}, f"unreachable mode: {mode!r}"
```

```go
// Test-only function living in prod code, but clearly marked as test-only - OK
func MakeTestSession() *Session { ... }
```

```python
# Unused symbol kept around "just in case" — remove
DEFAULT_TIMEOUT_SECONDS = 30  # not referenced anywhere
```

Type‑driven branches remove impossible cases:

```python
def handle(x: Bar | Baz | Quux) -> str:
    if isinstance(x, Bar):
        ...
    elif isinstance(x, Baz):
        ...
    elif isinstance(x, Quux):
        ...
    else:
        # Unreachable; keep at most a hard guard
        raise TypeError(f"Unexpected {type(x) = }")
```

## Exceptions

- Intentional extension points (plugin hooks, abstract interfaces) may appear unused locally but must be referenced by a registry, entry‑points, or configuration.
  Keep a short comment or link to the registry proving reachability:

  ```python
  def plugin_has_no_references_in_python():
      """Plugin, dynamically resolved from configuration YAML."""
      ...
  ```

  ```yaml
  # ~/.config/program/config.yaml
  active_plugins:
    - module_name:plugin_has_no_references_in_python
  ```

- Temporary compatibility shims may remain while a migration is in progress, with an owner and removal date

## Guidance

- Use local reasoning and established invariants. If a branch is obviously unreachable, delete it.
  When unsure, search references, check feature flags/config, and document the invariant you rely on.
- Prefer strengthening invariants and validations over keeping speculative fallback branches
