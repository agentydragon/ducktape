---
title: Barrel imports and public API (__init__/__all__)
kind: outcome
---

Barrel imports and re‑exports exist only to shape a deliberate, versioned public API. Internal modules/packages do not use barrel files; they import from the defining module explicitly. `__init__.py` files are empty (or have a trivial docstring) unless the package is a clearly public library surface, and `__all__` appears only when intentionally declaring that public API.

## Acceptance criteria (checklist)

- Internal code (non‑released, non‑SDK, not a clearly versioned library) MUST import from the module that defines a symbol; no “convenience” barrel imports.
- `__init__.py` in internal packages is empty or contains only a brief docstring/comment; no re‑exports.
- Re‑exports in `__init__.py` are allowed only for a deliberately public, stable API (e.g., SDK, framework, PyPI package):
  - The intent is documented inline (short comment/docstring: “Public API surface for package X”).
  - Re‑exports are selective and explicit (`from .foo import Client, Error`), not wildcard.
  - Private/internal modules/classes are not exported.
- `__all__`:
  - 99%+ of modules SHOULD NOT define `__all__`.
  - Allowed only in a package `__init__.py` that is deliberately curating the public API, with visible indicators (comment and explicit re‑exports) and stable ownership/versioning expectations.
  - Do not use `__all__` in internal modules as a convenience filter.
- Names are consistent and explicit: do not create generic aggregator modules like `shared`/`common` that mask true origins; import from the real module path.

## Positive examples

Explicit import from defining module (internal code):

```python
# aaa/x.py
def foo(): ...

# bbb/y.py
from aaa.x import foo
foo()
```

Deliberate public API in a serious, versioned library:

```python
# pkg/__init__.py
"""Public API surface for pkg (stable).

Exports: Client, Error.
"""
from .client import Client
from .errors import Error

__all__ = ["Client", "Error"]
```

## Negative examples

Internal convenience barrel in `__init__.py`:

```python
# internal_pkg/__init__.py   # ❌ internal package; do not re‑export
from .foo import *
from .bar import helper
```

Ambiguous aggregator import:

```python
from shared import foo   # ❌ where does this come from?
foo()                    # prefer: from real_module.submod import foo
```

Overuse of `__all__` in a normal module:

```python
# module.py  # ❌ not a curated public entrypoint
__all__ = [name for name in globals() if not name.startswith("_")]
```

## Notes

- Public API curation belongs at the package root `__init__.py` of a real library/SDK with versioning; everywhere else, keep imports explicit to preserve clear dependencies and call sites.
- Re‑exports should be rare, selective, and documented; wildcard exports and convenience barrels hinder traceability and refactoring.
- Related properties: [Imports at the top](./imports-top.md), [Truthfulness](../truthfulness.md) (comments must reflect real intent), [Consistent naming and notation](../consistent-naming-and-notation.md).
