---
title: Use yield fixtures for teardown
kind: outcome
---

Resources that require teardown should be provided via pytest yield fixtures; this is REQUIRED once the same setup/teardown appears in more than one test, otherwise recommended. Teardown must run in a `finally` block guarding the `yield`.

## Acceptance criteria (checklist)

- Yield fixtures are used for any resource needing teardown when the pattern is used in 2+ tests; single-use may inline cleanup, but prefer a yield fixture for clarity/reuse.
- Teardown lives in a `finally:` after the `yield`, ensuring cleanup on errors/failures and partial setups.
- No duplicated setup/teardown code across tests; factor into a fixture instead of copy/paste try/finally blocks.
- Prefer yield fixtures over `request.addfinalizer` for readability; use `addfinalizer` only when teardown must be registered conditionally or multiple independent cleanups are required.
- Fixture scope is chosen intentionally (function/module/session) and matches the resource lifetime; teardown corresponds to that scope.

## Positive examples

Basic resource with guaranteed cleanup:

```python
import pytest

@pytest.fixture
def temp_db(tmp_path):
    db = start_db(tmp_path)
    try:
        yield db
    finally:
        db.stop()
```

Parametrized fixture with per-case cleanup:

```python
@pytest.fixture(params=["v1", "v2"])
def api_server(tmp_path, request):
    srv = start_server(version=request.param, root=tmp_path)
    try:
        yield srv
    finally:
        srv.shutdown()
```

Using yield instead of duplicating cleanup in tests:

```python
# Good: fixture owns lifecycle

def test_reads(api_server):
    assert api_server.health() == "ok"

# (instead of repeating start/stop in each test)
```

## Negative examples

Duplicated try/finally across tests (factor into a fixture):

```python
# ❌ repeated in multiple tests
root = mktemp(); srv = start_server(root)
try:
    ...
finally:
    srv.shutdown(); rmtree(root)
```

Missing finally (cleanup skipped on failure):

```python
# ❌ teardown would be skipped if assertions fail
@pytest.fixture
def srv(tmp_path):
    srv = start_server(tmp_path)
    yield srv
    srv.shutdown()  # not guarded; prefer try/finally
```

## See also

- [Use pytest's standard fixtures for temp dirs and monkeypatching](./pytest-standard-fixtures.md)
