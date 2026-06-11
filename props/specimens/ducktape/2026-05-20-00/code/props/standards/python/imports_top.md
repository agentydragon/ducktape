---
title: Imports at the top
kind: outcome
---

All imports appear at the top of the module (not inside functions/classes); the only exception is a localized import used to break an otherwise unavoidable import cycle and must be documented with an inline comment.

## Acceptance criteria (checklist)

- No `import` or `from ... import ...` statements inside functions, methods, or class bodies
- Module-level imports are grouped at the top (after optional shebang/encoding line and module docstring)
- The only permitted in-function imports are narrowly justified cases and must include an inline comment explaining the reason: breaking an import cycle; dynamic runtime import by string (plugin discovery, `module:function` resolution) or hot-reload; or truly excessive import cost that would unacceptably degrade startup time

- Dynamic imports via `__import__` and `importlib.import_module` follow the same restriction; they are not allowed inside functions unless one of the allowed exceptions applies

## Positive examples

```python
"""Module docstring."""
from __future__ import annotations

import json
from pathlib import Path

def load_config(p: Path) -> dict:
    text = p.read_text()
    return json.loads(text)
```

## Negative examples

```python
def load_config(p):
    import json  # ❌ inline import (not a cycle)
    return json.loads(p.read_text())
```

```python
# ❌ import placed after executable code
print("starting up")
import logging
```

## Exceptions (narrow, justified)

Verified presence of certain listed unusual cases may justify a local import, but only with a verifiable AND accurate inline comment explaining the reason:

- Import cycle: comment must specifically describe the cycle a module-level import would create; prefer refactoring to remove the cycle when feasible.
- Heavy import: the module must be measurably expensive at import time and the localized import must materially reduce startup cost.
- Dynamic plugin/entrypoint or hot-reload: the behavior truly requires runtime import.
  Do not apply an exception if the module is already imported at the top elsewhere, the cost is negligible, or the cycle can be eliminated with a small refactor.

### Import cycle

```python
# file: foo/bar/service.py
def handler():
    # Avoid cyclical import: foo.bar.handler imports foo.baz.model → foo.quux.util → foo.bar.service
    from foo.bar import handler as upstream_handler
    return upstream_handler()
```

### Dynamic plugin or entrypoint import by string

```python
from importlib import import_module

def load_plugin(entrypoint: str):
    module_name, func_name = entrypoint.rsplit(":", 1)
    return getattr(import_module(module_name), func_name)
```

### Hot reload during development

```python
import myapp.config as config
importlib.reload(config)
```

### Deferring a heavy import

```python
def run_gpu_job():
    # Avoid import-time slowdown from compiling kernels (~30 s)
    import gigantic_cuda_lib
    return gigantic_cuda_lib.run()
```

## Additional negative examples

```python
def run_task(name: str):
    mod = __import__(name)  # ❌ dynamic import in function with no justification
    return mod.run()
```

```python
def run_task(name: str):
    mod = import_module(name)  # ❌ no plugin architecture/justification
    return mod.run()
```

### Misleading justification (still a violation)

These examples are additionally also [truthfulness](../truthfulness.md) violations.

```python
# mod_a.py
import os
import math

def fn_a():
    return os.listdir('.'), math.sqrt(2)

# mod_b.py
def compute_now():
    # avoid heavy import at import time
    import mod_a   # ❌ mod_a.py is NOT heavy - misleading - violation
    return mod_a.fn_a()
```

```python
# foo.py
import math
import datetime

from quux import xyzzy

def bar():
    ...

# baz.py
import quux

def fn():
    # local import to avoid cycle
    from foo import bar  # ❌ foo.py does not depend on baz.py - NOT a cycle. misleading - violation
```

### Nonspecific justification (still a violation)

```python
def compute_now():
    # avoid import loop
    import datetime
    return datetime.datetime.now()
```

## Cross-references

- [Truthfulness](../truthfulness.md): misleading "avoid cycle"/"heavy import" comments are untruthful when no cycle/heaviness exists; moving imports into functions can also misrepresent real dependency structure. Keep comments and structure honest about why an exception is taken.
