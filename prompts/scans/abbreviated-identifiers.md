# Scan: Abbreviated Identifiers

## Context
@../shared-context.md

## Overview

Python identifiers (field names, parameters, variables) should use **clear, descriptive names** that follow Google Python Style Guide conventions. Abbreviated or single-letter names are only acceptable in very limited contexts.

## Pattern: Unacceptable Abbreviations

### BAD: Abbreviated field names and parameters

```python
# BAD: Single-letter or unclear abbreviations
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._o = owner  # ✗ What is _o?
        self._n = name   # ✗ What is _n?

# BAD: Cryptic abbreviations in parameters
def process_request(req: Request, resp: Response, cfg: Config, ctx: Context):
    #                  ^^^  ^^^^  ^^^  ^^^
    # All unclear abbreviations

# BAD: Abbreviated loop variables (except i, j, k for indices)
for req in requests:  # ✗ Use full word 'request'
    for resp in req.responses:  # ✗ Use full word 'response'
        ...
```

### GOOD: Clear, descriptive names

```python
# GOOD: Full descriptive names
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._compositor = owner  # ✓ Clear what this is
        self._name = name         # ✓ Already clear

# GOOD: Full parameter names
def process_request(
    request: Request,
    response: Response,
    config: Config,
    context: Context
):
    ...

# GOOD: Clear loop variables
for request in requests:
    for response_item in request.responses:
        ...
```

## When Abbreviations Are Acceptable

### ✓ Mathematical/conventional single letters
```python
# OK: Standard mathematical conventions
for i in range(n):        # Loop index
    for j in range(m):    # Nested loop index
        matrix[i][j] = x * y + z  # Math variables

# OK: Established domain conventions
e = 2.71828...  # Euler's number
pi = 3.14159... # Pi
```

### ✓ Extremely short scopes (< 3 lines)
```python
# OK: Temporary in comprehension
items = [x for x in values if x > 0]

# OK: Lambda with obvious meaning
sorted_items = sorted(items, key=lambda x: x.value)

# OK: Unpacking with unused values
x, _, z = get_coordinates()  # _ for unused middle value
```

### ✓ Well-known abbreviations (sparingly)
```python
# Acceptable if universally understood in context
df = load_dataframe()  # Pandas convention
fh = open("file.txt")  # File handle (but prefer 'file')
```

## Detection Strategy

**Goal**: Find ALL abbreviated identifiers (>90% recall target).

**Approach**: Systematic AST analysis of all Python symbols

### Phase 1: Extract ALL identifiers

```python
import ast

def extract_all_identifiers(tree: ast.AST) -> dict[str, list[tuple[str, int]]]:
    """Extract every identifier from Python AST.

    Returns dict mapping identifier types to [(name, line_number), ...]
    """
    identifiers = {
        "class_fields": [],      # Class/instance attributes
        "parameters": [],        # Function parameters
        "local_variables": [],   # Local variable assignments
        "for_loop_vars": [],     # Loop iteration variables
    }

    class IdentifierVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            # Extract parameter names
            for arg in node.args.args:
                identifiers["parameters"].append((arg.arg, node.lineno))
            self.generic_visit(node)

        def visit_Assign(self, node):
            # Extract assignment targets
            for target in node.targets:
                if isinstance(target, ast.Name):
                    identifiers["local_variables"].append((target.id, node.lineno))
            self.generic_visit(node)

        def visit_For(self, node):
            # Extract loop variables
            if isinstance(node.target, ast.Name):
                identifiers["for_loop_vars"].append((node.target.id, node.lineno))
            self.generic_visit(node)

        def visit_Assign(self, node):
            # Extract class fields (self._field = ...)
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    if isinstance(target.value, ast.Name) and target.value.id == "self":
                        identifiers["class_fields"].append((target.attr, node.lineno))
            self.generic_visit(node)

    visitor = IdentifierVisitor()
    visitor.visit(tree)
    return identifiers
```

### Phase 2: Filter for abbreviations

```python
def is_abbreviated(name: str) -> bool:
    """Check if identifier is abbreviated.

    Abbreviated if:
    - 1-2 characters (except i, j, k, x, y, z in math contexts)
    - Ends with single letter abbreviation pattern (_o, _r, _c, etc.)
    - Contains vowel-removed abbreviations (cfg, ctx, req, resp, etc.)
    """
    # Single or double char (not math conventions)
    if len(name) <= 2 and name not in {"i", "j", "k", "x", "y", "z", "_", "id"}:
        return True

    # Ends with _X pattern (field abbreviation)
    if len(name) == 2 and name.startswith("_"):
        return True

    # Common abbreviations
    common_abbrevs = {
        "cfg", "config",  # Use 'configuration' or specific config name
        "ctx",            # Use 'context'
        "req",            # Use 'request'
        "resp",           # Use 'response'
        "param", "params",# Use 'parameter' or 'parameters'
        "arg", "args",    # Exception: args/kwargs are standard
        "val",            # Use 'value'
        "idx",            # Use 'index' or 'i'
        "msg",            # Use 'message'
        "tmp", "temp",    # Use descriptive temporary name
        "obj",            # Use specific object type
        "cls",            # OK in classmethods
        "exc",            # Use 'exception' or 'error'
        "err",            # Use 'error'
    }

    if name in common_abbrevs and name not in {"args", "kwargs", "cls"}:
        return True

    return False
```

### Phase 3: Filter out acceptable uses

```python
def is_acceptable_abbreviation(name: str, context: str) -> bool:
    """Check if abbreviation is acceptable in context."""
    # Math/loop indices
    if name in {"i", "j", "k", "x", "y", "z", "n", "m"} and "for" in context:
        return True

    # Standard conventions
    if name in {"df", "pd", "np"}:  # Pandas, NumPy conventions
        return True

    # Function parameter standards
    if name in {"args", "kwargs", "cls", "self"}:
        return True

    return False
```

### Automated scan command

```bash
# Find all 1-2 character field names (excluding i, j, k, _)
rg --type py '^\s+self\._[a-hln-z](?:\s*=|\s*:)'

# Find common abbreviations in parameters
rg --type py 'def \w+\([^)]*\b(cfg|ctx|req|resp|msg|tmp|obj)\b'

# Find all single-letter variables (excluding i, j, k in for loops)
rg --type py '\b[a-hln-z]\s*='
```

## Recall/Precision

- **Automated detection**: ~85-90% recall, ~70% precision
  - High recall because patterns are clear (length, common abbreviations)
  - Some false positives (acceptable abbreviations in math/conventions)
- **Manual filtering needed**: Review ~30% of candidates to eliminate acceptable uses

## Fix Strategy

### Priority 1: Field names (HIGH)
```python
# Before
self._o = owner
self._c = compositor
self._r = request

# After
self._compositor = owner  # or self._owner
self._compositor = compositor
self._request = request
```

### Priority 2: Parameters (HIGH)
```python
# Before
def process(req: Request, cfg: Config, ctx: Context):
    ...

# After
def process(request: Request, config: Config, context: Context):
    ...
```

### Priority 3: Local variables (MEDIUM)
```python
# Before
req = build_request()
resp = fetch(req)

# After
request = build_request()
response = fetch(request)
```

## Common Abbreviation Expansions

| Abbrev | Expand To | Notes |
|--------|-----------|-------|
| `cfg` | `config` or specific like `optimizer_config` | Prefer specific if multiple configs |
| `ctx` | `context` | Always expand |
| `req` | `request` | Always expand |
| `resp` | `response` | Always expand |
| `msg` | `message` | Always expand |
| `tmp` | Descriptive name | `temp_file`, `scratch_data`, etc. |
| `obj` | Specific type | `user_object`, `cache_entry`, etc. |
| `val` | `value` | Or more specific: `threshold_value`, `max_value` |
| `idx` | `index` or `i` | Use `i` for loop indices |
| `param` | `parameter` | Or specific: `query_parameter` |
| `exc` | `exception` or `error` | Prefer `error` in most cases |
| `_o` | `_owner` or specific | E.g., `_compositor`, `_parent` |
| `_r` | `_result` or specific | E.g., `_response`, `_record` |

## Benefits

✅ **Readability** - Code is self-documenting
✅ **Maintainability** - New contributors understand code faster
✅ **Searchability** - `request` is easier to search than `req`
✅ **IDE support** - Better autocomplete with full names
✅ **Consistency** - Follows Google Python Style Guide

## Examples from Codebase

```python
# ✗ BEFORE: Abbreviated
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._o = owner  # Cryptic!

# ✓ AFTER: Descriptive
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._compositor = owner  # Clear!
```

## References

- [Google Python Style Guide - Naming](https://google.github.io/styleguide/pyguide.html#s3.16-naming)
- [PEP 8 - Descriptive Naming Styles](https://peps.python.org/pep-0008/#descriptive-naming-styles)
- [Code Complete - Variable Naming Best Practices](https://www.oreilly.com/library/view/code-complete-2nd/0735619670/)
