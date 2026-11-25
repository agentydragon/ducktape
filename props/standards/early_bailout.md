---
title: Early bailout (guard clauses and loop guards)
kind: outcome
---

Functions and loops avoid unnecessary nesting by exiting early on failing preconditions; trivial top-level guards are expressed as early return/raise/continue/break, not as wrapping if-blocks.

## Acceptance criteria (checklist)

- Function guard: When a precondition fails and there is no corresponding else branch, use an early exit (return/raise) instead of wrapping the rest of the function in an if-block
- Multiple trivial guards: Sequential single-branch if-guards with no else are written as separate early exits (one per condition) or combined logically when clearer (e.g., `if not a or not b: return`), not as nested ifs
- Loop guard: When the first statement of a loop guards the entire body, use `continue` (or `break`) instead of wrapping the body in an if-block
- Error/flag pattern: Do not set sentinel flags and branch later; raise/return immediately at the detection site when no additional work is needed before exit
- With shared cleanup that would prevent early return, extract a helper so early exits are possible without duplicating cleanup
- Combine with "No unnecessary nesting" and walrus rules: flatten `if a: if b:` into `if a and b:` and use `:=` where it enables a single clear guard

## Examples

Guard a failing precondition at the point where the outcome is known:

```python
def load_user(uid: str) -> User:
    if not uid:
        raise ValueError("uid required")
    if not uid.startswith("u_"):
        raise ValueError("invalid uid")
    return repo.get(uid)
```

In a loop, continue past an ineligible item so the work on an eligible one stays together:

```python
for task in tasks:
    if not task.ready:
        continue
    run(task)
    log(task)
```

An early exit must still release acquired resources. A context manager or a
`finally` block can own the cleanup while the guarded operation returns.
