---
title: No unnecessary nesting (combine trivial guards)
kind: outcome
---

Trivial nested guards without else blocks are combined into a single condition; use logical conjunction (and/or) and the walrus operator to bind intermediate values when needed.

## Acceptance criteria (checklist)

- Patterns like `if a: if b:` (with no else between) are flattened to a single `if a and b:`
- Three+ level trivial nests (e.g., `if a: if b: if c:`) are flattened to a single combined condition
- When a nested guard exists only to reuse a freshly computed value, bind inline with `:=` and combine
- Deep nesting is acceptable only when branches have distinct else/elif flows or when readability clearly benefits

## Positive examples

```python
# Two-level flatten
if is_running and (code := proc.returncode) is not None:
    warn_failed(proc_id, code)
```

```python
# Three-level flatten with walrus
if user and user.active and (team := user.team) and team.enabled:
    grant_access(user, team)
```

```python
# Combine trivial guards
if item and item.ready and not item.error:
    process(item)
```

## Negative examples

```python
# Trivial else used to host the main body — should be a guard without else
if not req.auth:
    return unauthorized()
else:
    if not req.body:
        return bad_request()
    else:
        return ok(process(req.body))
```

```python
# Flattenable nested guards with elses — should combine/invert
if user:
    if user.active:
        grant_access(user)
    else:
        return
else:
    return
```

```python
# Trivial two-level nest — should be combined
if is_running:
    if proc.returncode is not None:
        warn_failed(proc_id, proc.returncode)
```

```python
# Trivial three-level nest — should be combined
if user:
    if user.active:
        if user.team:
            if user.team.enabled:
                grant_access(user, user.team)
```
