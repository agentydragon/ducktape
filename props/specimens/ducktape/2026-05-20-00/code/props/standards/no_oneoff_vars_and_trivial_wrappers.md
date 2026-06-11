---
title: No one-off variables or trivial pass-through wrappers
kind: outcome
---

Agent-edited code does not introduce single-use "one-off" variables that merely forward into the next call without adding non‑obvious value, and does not add pass‑through functions whose only behavior is to immediately call another function and return its result without a visible reason (e.g., boundary, adaptation, validation).

## Acceptance criteria (checklist)

- Single-use variables that simply forward into the next call are inlined, unless they convey non‑obvious meaning, are reused, or materially improve readability
- Functions that only call another function and return its result are absent, unless they add visible value (e.g., input normalization/validation, signature adaptation, dependency boundary, retries/backoff, structured logging/metrics, deprecation shim) and the reason is evident
- Test helpers/wrappers are acceptable when they encapsulate setup defaults or fixtures; public API adapters are acceptable when they adapt names/types/contracts (documented inline)
- Facade pass-throughs that stabilize an architectural boundary (e.g., App facade methods) are acceptable even if currently thin; include a brief docstring/comment stating the boundary and intent

## Negative examples (violations)

One-off iterator used only to feed collection:

```python
frames_iter = video.iter_frames()
frames = await collect_frames(frames_iter)
```

One-off error object immediately returned:

```python
error = FailureResponse(error="Not found", resource_id=rid)
return error.to_text_content()
```

Trivial pass-through wrapper with identical signature and call:

```python
def foo(a, b, c, d):
    return bar(a, b, c, d)
```

Trivial chain via one-off variables; should be one line:

```python
def probe_cache(namespace=None) -> bool:
    cfg = build_cache_config(namespace)
    client = cfg.make_client()
    return client.ready()
```

## Positive examples (acceptable)

Inline instead of one-off variable:

```python
await http.post_json({
    "type": "render_track",
    "data": [t.model_dump(exclude_none=True) for t in tracks],
})
```

Test helper encapsulates setup defaults (acceptable):

```python
def make_user(name: str = "Rai", email: str = "rai@example.com") -> User:
    return User(name=name, email=email)
```

### Negatives examples, fixed

Inline iterator usage:

```python
frames = await collect_frames(video.iter_frames())
```

Direct return of constructed value:

```python
return FailureResponse(error="Not found", resource_id=rid).to_text_content()
```

One-line chain:

```python
def probe_cache(namespace=None) -> bool:
    return build_engine_spec(snapshot_path).as_runner().ready()
```
