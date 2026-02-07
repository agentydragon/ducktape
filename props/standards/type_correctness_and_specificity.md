---
title: Type correctness and specificity
kind: behavior
---

Code is correctly and precisely typed.
Declared types reflect the actual values passed, returned, and assigned.
Types are neither too narrow nor too wide.
Unions reflect real control‑flow possibilities.
Prefer concrete types over `Any`.
Factor complex type expressions into named aliases.

## Acceptance criteria

- All assigned, returned, and passed values are members of their declared types. Static checks pass under chosen type checker where applicable.
- Types are not too narrow: no value violates its declared type at runtime or under static analysis.
- Types are not too wide: if a declaration uses a union (e.g., `A | B`), there must be a live, reachable code path producing/accepting each variant. Remove dead variants.
- Avoid `Any` when a reasonably precise type is feasible (e.g., `Callable[[X], Y] | Z`, concrete container element types, `Protocol`, `TypedDict` or generics).
- When interfaces use complex unions/algebraic types, use a type alias to keep signatures readable and DRY.
- Optionality is explicit: use `T | None` if and only if `None` is a real, reachable case.
- Avoid silencing type errors with blanket casts/ignores; fix the underlying types or add precise, documented narrows.

## Positive examples

Python (progressive typing):

```python
from collections.abc import Callable, Generator
from typing import TypedDict, Protocol, TypeAlias, Any

class Fetcher(Protocol):
    def __call__(self, url: str, timeout: float) -> bytes: ...

class Item(TypedDict):
    id: str
    size_bytes: int

def parse_items(raw: bytes) -> list[Item]:  # precise element type
    ...

# Concrete callable type instead of Any
def get_data(fetch: Fetcher) -> bytes:
    return fetch("/api", 2.5)

# Union with real paths for both variants
def load(kind: str) -> bytes | str:
    if kind == "bin":
        return b"\x00\x01"
    return "text"
```

Type alias removes repetition and clarifies meaning:

```python
ComplexInterfaceType: TypeAlias = (
    Callable[[int, str], bytes]
    | Generator[int, None, str]
)

def handle(handler: ComplexInterfaceType) -> str:
    if result := next(handler(1, "x"), None):
        return str(result)
    return "done"
```

## Negative examples

Too wide union (declares `int | str` but never returns `str` - should declare `-> int`):

```python
def f(flag: bool) -> int | str:
    return 1
```

Overuse of `Any` / loose type - should be `Callable[[int], str]` or similar:

```python
def run(cb: Any) -> Any:
    return cb(123)

def maybe() -> int | None:  # not actually optional
    return 3
```

Too narrow type (mismatched element types):

```python
users: list[str] = ["u1", 2]  # 2 is not str
```

Unnamed complex complex repeated in multiple places:

```python
from collections.abc import Callable, Generator

def g(h: Callable[[int, str], bytes] | Generator[int, None, str]) -> str:
    ...  # prefer to TypeAlias and DRY
```
