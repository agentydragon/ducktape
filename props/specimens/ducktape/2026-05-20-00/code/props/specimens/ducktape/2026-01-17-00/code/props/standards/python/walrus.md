---
title: Use walrus for trivial immediate conditions
kind: outcome
---

When a simple condition depends on a value computed immediately before, the value is bound inline with the walrus operator (:=) inside the condition.

## Acceptance criteria (checklist)

- Patterns like `if x`, `if not x`, `if x is None`, `if x is not None`, or `if x == <literal>` that depend on a freshly computed value use `:=` to bind inline
- The bound expression is the immediately evaluated value (e.g., a function call or awaitable)
- Do not create a separate one‑off variable assignment solely to feed the next `if` when `:=` would be equivalent and readable
- Do not force a walrus when the condition can be written directly without a temporary and the value is not reused (e.g., prefer `if server_process.poll() is not None:` over `if (_ := server_process.poll()) is not None:`)
- Only enforce when the walrus form remains a single, readable line after formatting

## Positive examples

```python
# DB lookup: bind inline for a trivial guard
if not (user := db.get(User, user_id)):
    return FailureResponse(error="User not found").to_text_content()

detail = DetailResponse(
    user_id=user_id,
    name=user.name,
    groups=[g.name for g in user.groups],
)
return detail.to_text_content()
```

```python
# Synchronous
if (item := cache.get(key)) is not None:
    return item
```

```python
# Equality to simple literal
if (code := compute_status()) == 1:
    handle_ok()
```

## Negative examples

```python
# One-off assignment only to feed the next if — should use walrus
user = db.get(User, user_id)
if not user:
    return FailureResponse(error="User not found").to_text_content()
```

```python
# Redundant two-step when a single `if (x := ...) is not None:` suffices
result = maybe_get()
if result is not None:
    use(result)
```

## Clarifications

- Apply this rule only to collapse a redundant two-step "assign, then immediately check" into a single `if` with `:=` when it improves clarity.
- Do not introduce throwaway bindings (e.g., `_ := ...`) just to satisfy the rule; either bind to a meaningful name you reuse, or write the condition directly.

## Dict error checks

### Positive examples

```python
# Use walrus to bind dict error payload inline
if error := resp.get("error"):
    raise ApiError(f"Server error: {error.get('message', 'unknown')}")
```

### Negative examples

```python
# Two-step then check — should use walrus
if "error" in resp:
    error = resp["error"]
    raise ApiError(f"Server error: {error.get('message', 'unknown')}")
```

## While reader loops

### Positive examples

```python
# File-like object
while chunk := f.read(8192):
    process(chunk)

# Async stream
while (line := await stream.readline()):
    handle(line)
```

### Negative examples

```python
# Two-step read loop instead of walrus
chunk = f.read(8192)
while chunk:
    process(chunk)
    chunk = f.read(8192)

# Async version
line = await stream.readline()
while line:
    handle(line)
    line = await stream.readline()
```
