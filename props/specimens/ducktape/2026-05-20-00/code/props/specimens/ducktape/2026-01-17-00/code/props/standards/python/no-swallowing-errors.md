---
title: Do not swallow errors (no silent except)
kind: outcome
---

Errors must not be silently ignored. Prefer letting exceptions propagate to a proper boundary; when catching is required, catch specific exceptions, take a concrete action, and/or surface the issue (UI or logs). Blanket catches with no action are banned, including in tests.

## Acceptance criteria (checklist)

- Absolute ban: `except Exception: pass` and any blanket catch that does nothing (including tests) — remove or replace with specific handling.
- Prefer no local handling at all (let it crash) unless there is a clear, domain‑specific recovery or boundary responsibility; this is the correct choice the vast majority of the time (≈90%+).
- When catching, scope the `try` narrowly and catch specific, expected exception types only; do not mask unrelated errors.
- Surfacing requirement: if you ignore an error by design, surface it appropriately — ideally to the user (UI/output) or at least log with context; document the rationale.
- No silent failures during teardown/shutdown; teardown errors must be logged (and re‑raised when appropriate for the boundary).
- Tests must not hide exceptions with broad catches; failures should fail the test unless the test is explicitly asserting the exception with `pytest.raises`.

## Positive examples

Let exceptions propagate (no swallowing):

```python
def load_config(path: Path) -> dict:
    text = path.read_text()            # may raise; OK to bubble up here
    return json.loads(text)            # may raise; OK to bubble up here
```

Catch specific error, log, then re‑raise (or return a safe default when explicitly acceptable):

```python
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    logger.error("Invalid JSON", preview=text[:100])
    raise
```

Specific no‑op with API that encodes the intent (prefer over catching exceptions):

```python
# OK: explicit no‑op on existent dirs
path.mkdir(parents=True, exist_ok=True)
```

Test asserts the expected exception (no swallowing):

```python
with pytest.raises(FileNotFoundError):
    _ = Path("/no/such").read_text()
```

Teardown with guaranteed logging:

```python
@pytest.fixture
def server(tmp_path):
    srv = start_server(tmp_path)
    try:
        yield srv
    finally:
        try:
            srv.shutdown()
        except Exception:
            logger.exception("Server shutdown failed")
            # Boundary decision: re‑raise here if failures must fail tests/jobs
```

## Negative examples

Blanket swallow (always banned):

```python
try:
    do_thing()
except Exception:
    pass  # ❌ silent swallow
```

Teardown swallowing errors silently (logs missing):

```python
try:
    cleanup()
except Exception:  # ❌ do not hide teardown errors
    return
```

Tests masking real failures:

```python
def test_something():
    try:
        run_code()
    except Exception:
        pass  # ❌ hides failures; use pytest.raises for specific exceptions
```

Catching the wrong thing (masks other issues):

```python
try:
    os.remove(p)
except Exception:  # ❌ should catch FileNotFoundError if ignoring that case only
    logger.info("ignored")
```

## Exceptions (narrow)

- Legitimate no‑op outcomes should use APIs that encode the no‑op instead of exceptions (e.g., `mkdir(exist_ok=True)`, `dict.get`, idempotent delete with specific `FileNotFoundError` catch). If you must catch, catch only the specific exception and include a short rationale.
- At true outer boundaries (HTTP handlers, main loops), a broad catch may be used to convert to an error response — must log with full context (`logger.exception`) and avoid continuing in a corrupted state.

## See also

- [Try/except is scoped around the operation it guards](./scoped-try-except.md)
