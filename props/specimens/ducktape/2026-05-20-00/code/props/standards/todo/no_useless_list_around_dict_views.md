# Property: No Useless list() Around Dict Views

**Status:** Planned
**Kind:** outcome

## Predicate

Forbid useless `list(...)` around dict views in loops when not mutating the dict during iteration.

## Rationale

When iterating over dict views (`.keys()`, `.values()`, `.items()`), wrapping them in `list()` is unnecessary unless you need to mutate the dictionary during iteration. The overhead of creating a list copy is wasteful.

## Acceptance Criteria

- No `list(dict.values())` in for loops when the dict is not mutated during iteration
- No `list(dict.keys())` or `list(dict.items())` similarly
- Allow `list()` when the dict IS mutated during iteration (document why)

## Example (from TODO.md)

**Bad:**

```python
while True:
    for worker in list(self.workers.values()):
        if worker.proc and worker.status == JobStatus.RUNNING:
            if worker.proc.returncode is not None:
                logger.warning(
                    "Worker process died unexpectedly",
                    worker_id=worker.id,
                    return_code=worker.proc.returncode,
                )
                worker.status = JobStatus.FAILED
```

**Good:**

```python
# Iterate directly over the view when not mutating the dict
for worker in self.workers.values():
    # ...same logic...
```

## Detection Strategy

- AST check for `Call(func=Name(id='list'), args=[Call(func=Attribute(attr='values'|'keys'|'items'))])`
- Check if dict is mutated in loop body (conservative: flag for review even if uncertain)
