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

## Positive examples

```python
# Function guard: fail fast on preconditions
def load_user(uid: str) -> User:
    if not uid:
        raise ValueError("uid required")
    if not uid.startswith("u_"):
        raise ValueError("invalid uid")
    return repo.get(uid)
```

```python
# Loop guard: continue instead of wrapping entire body
for job in jobs:
    if not job.ready:
        continue
    process(job)
```

```python
# Combine trivial guards with walrus
if (rec := get_record(key)) is None or rec.error:
    return None
return rec.value
```

```js
// JS: early returns instead of nested ifs
function handle(req) {
  if (!req.auth) return unauthorized();
  if (!req.body) return badRequest();
  return ok(process(req.body));
}
```

```python
# Shared cleanup via helper enables early bailout
conn = connect()
try:
    def _do():
        if not request.valid:
            return None
        if not has_perm(request.user):
            return None
        return serve(request)
    result = _do()
finally:
    conn.close()
```

## Negative examples

```python
# Trivial else wrapping the happy path — should early‑return
def handle(req):
    if not req.auth:
        return unauthorized()
    else:
        # Entire body is under else
        result = process(req)
        return ok(result)
```

```python
# Loop uses else to skip — should guard with continue
for task in tasks:
    if task.ready:
        run(task)
        log(task)
    else:
        continue
```

```python
# Function wrapped by a trivial if — should be early return
def load_user(uid: str) -> User | None:
    if uid:
        user = repo.get(uid)
        return user
```

```python
# Nested trivial guards — should be flattened or early-exited
def save(item):
    if item:
        if item.valid:
            if not item.error:
                return persist(item)
```

```python
# Loop body wrapped — should use continue
for task in tasks:
    if task.ready:
        run(task)
        log(task)
```

```python
# Error flag used later — should raise immediately
ok = True
for part in parts:
    if part.invalid:
        ok = False
# ... many lines later ...
if not ok:
    raise ValueError("invalid part")
```
