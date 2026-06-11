---
title: Modern type hints (PEP 604 unions, builtin generics)
kind: outcome
---

Agent-edited Python uses modern typing: builtin generics (e.g., `list[int]`) and PEP 604 unions (`A | B`), not legacy `typing.List`, `typing.Dict`, `typing.Union`, or `typing.Optional`.

## Acceptance criteria (checklist)

- Builtin generics are used: `list[T]`, `dict[K, V]`, `set[T]`, `tuple[T, ...]`
- Unions use `A | B` and optional uses `T | None`
- No `typing.Union`, `typing.Optional`, `typing.List`, `typing.Dict`, `typing.Set`, `typing.Tuple` in edited hunks
- Forward references do not use string type names when `from __future__ import annotations` can be used
- It is acceptable to target an older Python that lacks these features only when positively identified as the target

## Positive examples

```python
from __future__ import annotations
from collections.abc import Iterable

def names(items: Iterable[str] | None) -> list[str]:
    return [x for x in (items or [])]
```

```python
UserRecord = dict[str, str | int]
ids: set[int] = {1, 2, 3}
```

## Negative examples

```python
from typing import List, Dict, Optional, Union

def names(items: Optional[List[str]]) -> List[str]:
    return [x for x in (items or [])]
```

```python
from __future__ import annotations

class Node:
    def child(self) -> "Node":  # ❌ string forward ref; use future annotations instead of strings
        ...
```
