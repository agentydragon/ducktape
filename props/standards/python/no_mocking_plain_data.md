---
title: Prefer real data/objects over mocks (do not mock plain data)
kind: outcome
---

Do not mock plain data or trivially constructible domain models. Use real objects and real resources (tmp filesystem, real Pydantic models) where practical; reserve mocks/stubs for hard boundaries (network, time, processes) or truly expensive/unavailable dependencies.

## Acceptance criteria (checklist)

- Banned: mocking trivial data containers (e.g., Pydantic models, simple dataclasses, plain dicts) by setting attributes on `Mock/MagicMock`. Construct real instances instead.
- Prefer real filesystem under `tmp_path`/`tmp_path_factory` over broad monkeypatching of `os`/`pathlib` across modules.
- Mock/stub only at external boundaries or costly/unreliable layers (HTTP, DB connections, time, randomness); keep scope narrow and specific.
- Builders/factories provide realistic defaults for domain models; validation must pass.
- Mocks never mask schema/type errors; do not “shape” mocks to look like models to placate type checks.
- Use dependency injection (pass collaborators) so tests can supply small fakes for interfaces; avoid patching internals when a constructor parameter would suffice.

## Positive examples

Construct a real Pydantic model (no MagicMock):

```python
from pydantic import BaseModel, ConfigDict

class User(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    email: str

u = User(id="u_123", email="u@example.com")  # ✅ real, validated instance
```

Real filesystem with tmp_path:

```python
def test_writer(tmp_path: Path):
    out = tmp_path / "data.txt"
    write_data(out, "hello")
    assert out.read_text() == "hello"
```

Boundary‑level mock of HTTP only (payload realistic):

```python
def test_fetch_user(client, requests_mock):
    requests_mock.get("https://api.example.com/users/u_123", json={
        "id": "u_123", "email": "u@example.com"
    })
    assert client.fetch_user("u_123").id == "u_123"
```

## Negative examples

Mocking a Pydantic model (banned):

```python
# ❌ do not do this
user = MagicMock()
user.id = "u_123"
user.email = "x@example.com"
service.handle(user)
```

Mocking core filesystem APIs for an entire module:

```python
# ❌ prefer real tmp_path over patching pathlib/os globally
monkeypatch.setattr("mymod.pathlib.Path", FakePath)
```

Shaping a dict with wrong keys/types instead of constructing real data:

```python
# ❌ incorrect shape; bypasses validation
payload = {"userId": 1, "mail": "x"}
process(payload)
```

## Exceptions (narrow)

- Mocking is acceptable when the object cannot be constructed in tests without heavy external state (e.g., real DB connection, complex binary handles) and when the test specifically targets the interaction contract; keep mocks minimal and focused on the boundary.
- For non‑plain fields that are impractical to instantiate (e.g., embedded OS handles), provide small fakes implementing only the required interface.

## See also

- [Use pytest's standard fixtures for temp dirs and monkeypatching](./pytest-standard-fixtures.md)
- [Use yield fixtures for teardown](./pytest-yield-fixtures.md)
- [Structured data types over untyped mappings](../structured-data-over-untyped-mappings.md)
- [No useless tests](../no-useless-tests.md)
