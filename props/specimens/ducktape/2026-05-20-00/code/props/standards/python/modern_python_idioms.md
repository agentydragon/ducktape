---
title: Prefer modern Python idioms (operators, types)
kind: outcome
---

Use modern Python 3.11+ idioms that improve clarity and brevity: dict merge operators, set operators, PEP 604 union types, and related conveniences. Prefer these over legacy patterns.

## Acceptance criteria (checklist)

- Dictionaries:
  - Use merge and update operators (PEP 584): `a | b` and `a |= b` (right side wins on key conflicts). Avoid `{**a, **b}` or manual loops for merging.
- Sets:
  - Use operator forms for set algebra: `|` (union), `&` (intersection), `-` (difference), `^` (symmetric difference), and their in‑place variants `|=`, `&=`, `-=`, `^=`. Avoid verbose method chains when simple operators suffice.
- Type hints:
  - Use union types with `|` (PEP 604): `A | B | C` instead of `Union[A, B, C]`.
  - Prefer `Self`/`from __future__ import annotations` patterns as needed (see [Type hints](./type-hints.md)).
- isinstance/issubclass:
  - Continue to use tuples for multiple types: `isinstance(x, (A, B, C))`.
  - Do NOT write `isinstance(x, A | B)` — union types are for annotations, not for runtime checks.
- Strings:
  - Prefer `str.removeprefix/suffix` over slicing for safety and intent (see [String affixes](./str-affixes.md)).
- Pattern matching:
  - Consider `match/case` for simple tag dispatch or structural cases when it improves readability over long `if/elif` chains.

## Positive examples

Dict merge/update (right wins):

```python
cfg = base_cfg | override_cfg
cfg |= env_cfg
```

Set algebra with operators:

```python
missing = required - present
common = a & b
all_tags = a | b | c
```

Union types in annotations:

```python
from typing import Self

def parse(val: int | str) -> int: ...

class X:
    def clone(self) -> Self: ...
```

String affixes:

```python
name = name.removesuffix(".json")
```

## Negative examples

Legacy dict merge patterns:

```python
merged = {**a, **b}              # ❌ prefer a | b
for k, v in b.items():           # ❌ manual merge
    a[k] = v
```

Verbose set API where operators are clearer:

```python
s = a.union(b, c)                # ❌ prefer a | b | c
s = a.intersection(b)            # ❌ prefer a & b
```

Union types misused at runtime:

```python
if isinstance(x, A | B):         # ❌ not supported for isinstance; use a tuple
    ...
```

Old typing style:

```python
from typing import Union

def f(x: Union[int, str]) -> int:  # ❌ prefer int | str
    ...
```

## Notes

- Readability first: prefer these idioms when they clarify intent and reduce noise; if an operator would obscure meaning in a complex expression, a named helper or method call can be acceptable.
- Related properties: [Walrus operator](./walrus.md), [String affixes](./str-affixes.md), [Type hints](./type-hints.md), [Pathlib usage](./pathlib.md).
