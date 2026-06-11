---
title: No useless tests
kind: outcome
---

Tests must provide distinct value by exercising production behavior or documenting non‑obvious ground truth; redundant or trivially satisfied tests are removed or consolidated.

## Acceptance criteria (checklist)

- No change‑detector tests that merely assert constants, enum values, or literals (e.g., "SomeEnum.VALUE == 'value'") unless the test documents an important, non‑obvious dependency behavior we explicitly rely on (state that rationale inline).
- No tests that only re‑assert trivial, widely‑known properties of standard library or ubiquitous dependencies (e.g., Pydantic BaseModel.model_dump_json() returns str, pathlib.Path.name is a str). Exception: pinning a known upstream regression/workaround — include a clear inline rationale and link to the upstream issue.
- Parameterization or subtests cover representative classes of inputs; avoid enumerating duplicative cases that exercise the exact same behavior/path (e.g., (1,2,3) and (2,1,3) for commutative addition) unless the additional case tests a distinct property.
- Remove assertions that are implied by stronger ones (e.g., avoid `assert x is not None` when `isinstance(x, Foo)` and `x.bar == 100` already guarantee non‑None).
- Do not keep tests fully subsumed by other tests at the same abstraction level; consolidate into one parametrized test or delete the subset test.
- Each test should either:
  - Exercise production code (not just in‑module constants/types), or
  - Demonstrate a non‑obvious, important behavior of a dependency (clearly documented in the test),
    and otherwise be removed.
- Overlap across abstraction levels (e2e vs unit) is acceptable; duplication is only a violation when a test adds no new behavior coverage or rationale at its level.

## Positive examples

Parametrized, representative coverage (no duplicative cases):

```python
import pytest

@pytest.mark.parametrize(
    "a,b,expected",
    [
        (0, 0, 0),          # boundary
        (1, 2, 3),          # representative positive
        (-1, 2, 1),         # sign mix
    ],
)

def test_add_representative(a, b, expected):
    assert add(a, b) == expected
```

Non‑redundant focused assertions:

```python
x = output_of_prod_code()
assert isinstance(x, Foo)
assert x.bar == 100
```

## Negative examples

Change‑detector (no behavior exercised):

```python
def test_enum_value():
    assert SomeEnum.VALUE == "value"
```

Fully subsumed single‑case duplication:

```python
def test_add_8_8():
    assert add(8, 8) == 16  # subsumed by parametrized representative cases
```

Redundant implied assertion:

```python
x = output_of_prod_code()
assert x is not None      # useless: implied by the next two
assert isinstance(x, Foo)
assert x.bar == 100
```

Trivial property of a common dependency API:

```python
# Pydantic's API already guarantees a JSON string here; this adds no value
# Use such a test only to pin an upstream regression and include rationale+link
from hamcrest import assert_that, instance_of

def test_model_dump_json_type_is_string():
    assert_that(user.model_dump_json(), instance_of(str))  # useless
# or
# def test_model_dump_json_type_is_string():
#     assert isinstance(user.model_dump_json(), str)  # useless
```
