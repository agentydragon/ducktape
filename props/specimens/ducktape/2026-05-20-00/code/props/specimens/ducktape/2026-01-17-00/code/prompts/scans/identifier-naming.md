# Scan: Identifier Naming

## Context

@../shared-context.md

## Core Principle

**Naming clarity should scale with identifier lifespan**: The longer an identifier lives and the broader its scope, the more readable and unambiguous its name must be.

| Lifespan                    | Examples                           | Naming Requirements                                                     |
| --------------------------- | ---------------------------------- | ----------------------------------------------------------------------- |
| **Momentary** (1-3 lines)   | Loop vars, comprehensions, lambdas | Brief names OK: `i`, `j`, `k`, `x`, `_`                                 |
| **Short** (< 10 lines)      | Local variables in tight scopes    | Context-dependent: `val` OK if clear, prefer `threshold_value` if not   |
| **Medium** (function scope) | Parameters, local variables        | Clear descriptive names: `request` not `req`, `config` not `cfg`        |
| **Long** (class/module)     | Class fields, module globals       | Explicit and unambiguous: `_compositor` not `_o`, `cache_key` not `key` |

## Overview

Python identifiers should follow **Google Python Style Guide conventions**. Two main antipatterns:

1. **Abbreviated identifiers**: Cryptic shortened names (`cfg`, `ctx`, `req`, `_o`)
2. **Vague identifiers**: Generic names that lack context (`id`, `key`, `name` in generic containers)

Both are acceptable in very limited contexts (momentary lifespans, strong conventions).

---

## Antipattern 1: Abbreviated Identifiers

### BAD: Abbreviated names in long-lived scopes

```python
# BAD: Class fields (long-lived)
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._o = owner  # ✗ What is _o?
        self._n = name   # ✗ What is _n?

# BAD: Function parameters (medium-lived)
def process_request(req: Request, resp: Response, cfg: Config, ctx: Context):
    #                  ^^^  ^^^^  ^^^  ^^^ All unclear abbreviations
    ...

# BAD: Loop variables for non-indices (short-lived but unclear)
for req in requests:  # ✗ Use full word 'request'
    for resp in req.responses:  # ✗ Use full word 'response'
        ...
```

### GOOD: Full descriptive names for medium+ lifespans

```python
# GOOD: Class fields - full descriptive names
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._compositor = owner  # ✓ Clear what this is
        self._name = name         # ✓ Already clear

# GOOD: Function parameters - full names
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

### When Abbreviations Are Acceptable

#### ✓ Momentary scope (< 3 lines)

```python
# OK: Mathematical/index conventions
for i in range(n):        # Loop index
    for j in range(m):    # Nested loop index
        matrix[i][j] = x * y + z

# OK: Comprehension temporary
items = [x for x in values if x > 0]

# OK: Lambda with obvious meaning
sorted_items = sorted(items, key=lambda x: x.value)

# OK: Unpacking with unused values
x, _, z = get_coordinates()  # _ for unused middle value
```

#### ✓ Well-established conventions (use sparingly)

```python
# Acceptable if universally understood
df = load_dataframe()  # Pandas convention
args, kwargs           # Standard Python conventions
cls, self              # Standard Python conventions
```

### Common Abbreviation Expansions

| Abbrev  | Expand To                                    | Notes                                            |
| ------- | -------------------------------------------- | ------------------------------------------------ |
| `cfg`   | `config` or specific like `optimizer_config` | Prefer specific if multiple configs              |
| `ctx`   | `context`                                    | Always expand for medium+ lifespans              |
| `req`   | `request`                                    | Always expand for medium+ lifespans              |
| `resp`  | `response`                                   | Always expand for medium+ lifespans              |
| `msg`   | `message`                                    | Always expand for medium+ lifespans              |
| `tmp`   | Descriptive name                             | `temp_file`, `scratch_data`, etc.                |
| `obj`   | Specific type                                | `user_object`, `cache_entry`, etc.               |
| `val`   | `value`                                      | Or more specific: `threshold_value`, `max_value` |
| `idx`   | `index` or `i`                               | Use `i` for momentary loop indices               |
| `param` | `parameter`                                  | Or specific: `query_parameter`                   |
| `exc`   | `exception` or `error`                       | Prefer `error` in most cases                     |
| `_o`    | `_owner` or specific                         | E.g., `_compositor`, `_parent`                   |
| `_r`    | `_result` or specific                        | E.g., `_response`, `_record`                     |

---

## Antipattern 2: Vague Identifiers

Field names should be **explicit and self-documenting within their usage context**. This is **semantic analysis**, not pattern matching.

### BAD: Generic container + vague field

```python
# BAD: "Response" is too generic - response to what? Which ID?
class Response(BaseModel):
    key: str  # Hash? Database key? API key? Cache key?
    id: str   # ID of what? Request? Response? User?
    name: str # Name of what?

# BAD: Multiple configs in scope - which one?
def process_rollout(rollout, task, cfg, grading_cfg, model_cfg):
    cfg.get_value()  # Which config? Optimizer? Task? Model?
```

### GOOD: Specific container OR specific field names

```python
# GOOD: Container name makes it obvious (long-lived)
class User(BaseModel):
    id: int        # Obviously user_id due to class name
    name: str      # Obviously user name due to class name

class CacheEntry(BaseModel):
    key: str       # Obviously cache key due to class name

# GOOD: Only one config in scope (medium-lived)
def validate_config(cfg: OptimizerConfig) -> bool:
    return cfg.validate()  # Only one cfg, abbreviation acceptable

# GOOD: Multiple configs? Use specific names (medium-lived)
def process_rollout(
    rollout,
    task,
    optimizer_config,   # ✓ Specific (or optimizer_cfg if only config of this type)
    grading_config,     # ✓ Specific
    model_config        # ✓ Specific
):
    optimizer_config.get_value()  # ✓ Clear which config
```

### A Name is Vague When

- **Context doesn't clarify its purpose** (e.g., `cfg` in a function with multiple config objects)
- **Multiple similar entities exist** (e.g., `id` when there are user IDs, request IDs, etc. nearby)
- **The containing scope is generic** (e.g., `key` in a class called `Response`)

### A Name is NOT Vague When

- **Container name provides full context** (e.g., `User.id` is obviously the user's ID)
- **Only one of its kind exists** in the scope (e.g., single `status` field in focused class)
- **Convention makes it obvious** (e.g., SQLAlchemy model's `id` primary key)
- **Lifespan is momentary** (e.g., `key, value` in tight loop: `for key, value in items:`)

### Common Vague Names (Context-Dependent)

| Name            | Vague When                                | Clear When                                  |
| --------------- | ----------------------------------------- | ------------------------------------------- |
| `id`            | Generic class like `Response`, `Data`     | Specific model like `User.id`, `Product.id` |
| `name`          | Generic class, multiple name types nearby | Specific model like `Category.name`         |
| `key`           | Generic class, unclear which type of key  | `CacheEntry.key`, `EncryptionContext.key`   |
| `data`          | Passed around, unclear what it contains   | Single data field in focused class          |
| `cfg`, `config` | Multiple configs in same scope            | Only config in scope                        |
| `value`         | Generic getter/setter                     | `ThresholdConfig.value`, `Setting.value`    |
| `type`          | Without discriminated union context       | `type: Literal["user", "admin"]` in union   |

---

## Detection Strategy

### Phase 1: Extract ALL identifiers

**Goal**: Find ALL naming issues (>90% recall target for abbreviations, 100% for vague in manual review).

```python
import ast

def extract_all_identifiers(tree: ast.AST) -> dict[str, list[tuple[str, int, str]]]:
    """Extract every identifier from Python AST.

    Returns dict mapping identifier types to [(name, line_number, context), ...]
    where context is the containing class/function name for scope analysis.
    """
    identifiers = {
        "class_fields": [],      # Class/instance attributes (LONG-LIVED)
        "parameters": [],        # Function parameters (MEDIUM-LIVED)
        "local_variables": [],   # Local variable assignments (SHORT-LIVED)
        "for_loop_vars": [],     # Loop iteration variables (MOMENTARY)
    }

    class_stack = []  # Track nesting for container name context
    function_stack = []

    class IdentifierVisitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            class_stack.append(node.name)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node):
            function_stack.append(node.name)
            # Extract parameter names (MEDIUM-LIVED)
            context = f"{'.'.join(class_stack)}.{node.name}" if class_stack else node.name
            for arg in node.args.args:
                if arg.arg not in ("self", "cls"):
                    identifiers["parameters"].append((arg.arg, node.lineno, context))
            self.generic_visit(node)
            function_stack.pop()

        def visit_Assign(self, node):
            context = f"{'.'.join(class_stack)}.{'.'.join(function_stack)}" if class_stack or function_stack else "module"
            # Extract assignment targets
            for target in node.targets:
                if isinstance(target, ast.Name):
                    identifiers["local_variables"].append((target.id, node.lineno, context))
                elif isinstance(target, ast.Attribute):
                    # Class fields (self._field = ...)
                    if isinstance(target.value, ast.Name) and target.value.id == "self":
                        identifiers["class_fields"].append((target.attr, node.lineno, '.'.join(class_stack)))
            self.generic_visit(node)

        def visit_For(self, node):
            context = f"{'.'.join(class_stack)}.{'.'.join(function_stack)}" if class_stack or function_stack else "module"
            # Extract loop variables (MOMENTARY)
            if isinstance(node.target, ast.Name):
                identifiers["for_loop_vars"].append((node.target.id, node.lineno, context))
            self.generic_visit(node)

    visitor = IdentifierVisitor()
    visitor.visit(tree)
    return identifiers
```

### Phase 2: Filter for Issues

```python
def is_abbreviated(name: str, lifespan: str) -> bool:
    """Check if identifier is unacceptably abbreviated for its lifespan.

    Args:
        name: Identifier name
        lifespan: One of "momentary", "short", "medium", "long"

    Returns:
        True if abbreviated and should be expanded
    """
    # Momentary lifespan: most abbreviations acceptable
    if lifespan == "momentary":
        # Even in momentary scope, avoid cryptic abbreviations
        return name in {"cfg", "ctx", "req", "resp"} and len(name) <= 3

    # Standard conventions always OK
    if name in {"args", "kwargs", "cls", "self", "df", "pd", "np"}:
        return False

    # Mathematical conventions OK in short+ scopes
    if lifespan == "short" and name in {"i", "j", "k", "x", "y", "z", "n", "m"}:
        return False

    # Single or double char (not math conventions) - problematic in medium+ lifespans
    if len(name) <= 2 and name not in {"i", "j", "k", "x", "y", "z", "_", "id"}:
        return lifespan in ("medium", "long")

    # Ends with _X pattern (field abbreviation) - always problematic in long-lived
    if lifespan == "long" and len(name) == 2 and name.startswith("_"):
        return True

    # Common abbreviations - problematic in medium+ lifespans
    common_abbrevs = {
        "cfg", "ctx", "req", "resp", "msg", "tmp", "temp",
        "obj", "val", "idx", "param", "exc", "err"
    }

    if name in common_abbrevs:
        return lifespan in ("medium", "long")

    return False


def is_vague(name: str, context: dict, lifespan: str) -> bool:
    """Check if identifier is vague for its context.

    This requires semantic analysis - automation has low recall (~30%).

    Args:
        name: Identifier name
        context: Dict with keys:
            - container_name: Name of containing class (if any)
            - scope_entities: Other similar identifiers in scope
            - is_model_field: Whether this is a Pydantic/dataclass field
        lifespan: One of "momentary", "short", "medium", "long"

    Returns:
        True if likely vague (requires manual verification)
    """
    # Momentary lifespan: vagueness acceptable
    if lifespan == "momentary":
        return False

    # Common potentially-vague names
    vague_candidates = {"id", "key", "name", "data", "value", "type", "status"}

    if name not in vague_candidates:
        return False

    container = context.get("container_name", "")
    similar_entities = context.get("scope_entities", [])

    # Generic container + vague field = problem
    generic_containers = {"Response", "Data", "Result", "Item", "Entry", "Record"}
    if container in generic_containers:
        return True

    # Multiple similar entities in scope = problem
    if len([e for e in similar_entities if name in e]) > 1:
        return True

    # Specific container = probably fine
    specific_containers = {
        "User", "Product", "Category", "CacheEntry",
        "Config", "Settings", "Credentials"
    }
    if any(container.endswith(s) for s in specific_containers):
        return False

    # Otherwise: requires manual judgment
    return None  # Inconclusive - manual review needed
```

### Phase 3: Prioritize by Lifespan

```python
def prioritize_issues(identifiers: dict) -> list[tuple[str, int, str, str]]:
    """Prioritize naming issues by lifespan (long-lived first).

    Returns list of (name, line, context, issue_type) sorted by priority.
    """
    issues = []

    # PRIORITY 1: Long-lived (class fields)
    for name, line, context in identifiers["class_fields"]:
        if is_abbreviated(name, "long"):
            issues.append((name, line, context, "abbreviated_field", 1))
        # Note: vague field detection requires manual review with full class definition

    # PRIORITY 2: Medium-lived (parameters)
    for name, line, context in identifiers["parameters"]:
        if is_abbreviated(name, "medium"):
            issues.append((name, line, context, "abbreviated_param", 2))

    # PRIORITY 3: Short-lived (local variables) - only if clearly abbreviated
    for name, line, context in identifiers["local_variables"]:
        if is_abbreviated(name, "short"):
            issues.append((name, line, context, "abbreviated_local", 3))

    # PRIORITY 4: Momentary (loop vars) - rarely issues
    # (Only flag if extremely cryptic)

    return sorted(issues, key=lambda x: x[4])  # Sort by priority
```

### Automated Scan Commands

```bash
# Find all 1-2 character field names (excluding i, j, k, _)
rg --type py '^\s+self\._[a-hln-z](?:\s*=|\s*:)'

# Find common abbreviations in parameters
rg --type py 'def \w+\([^)]*\b(cfg|ctx|req|resp|msg|tmp|obj|val|idx|param|exc|err)\b'

# Find potentially vague field names in generic containers (HIGH false positive rate)
rg --type py 'class (Response|Data|Result|Item|Entry)\(' -A 10 | rg '^\s+(id|key|name|data|value|type):'

# Find functions with multiple similar parameters (e.g., multiple IDs)
rg --type py 'def \w+\([^)]*\bid\b[^)]*\bid\b'
```

### Manual Review (Required for Vague Names)

**Vague name detection requires semantic analysis** (automation recall ~30-40%):

1. **Read model definitions** - Generic containers with generic fields:
   - `Response.id`, `Data.key`, `Result.name` → likely vague
   - `User.id`, `CacheEntry.key`, `Product.name` → likely fine

2. **Read function signatures** - Multiple similar entities:

   ```python
   # BAD: Three IDs in scope
   def link_items(id: str, parent_id: str, user_id: str):
       process(id)  # Which ID?
   ```

3. **Analyze scope complexity** - Abbreviations in complex contexts:
   - `cfg` in function with 5 config types → vague
   - `cfg` as only config → acceptable

---

## Fix Strategy

### Priority 1: Class Fields (LONG-LIVED) - HIGH

```python
# Before
self._o = owner
self._c = compositor
self._r = request

# After
self._compositor = owner  # or self._owner if more general
self._compositor = compositor
self._request = request
```

### Priority 2: Parameters (MEDIUM-LIVED) - HIGH

```python
# Before
def process(req: Request, cfg: Config, ctx: Context):
    ...

# After
def process(request: Request, config: Config, context: Context):
    ...
```

### Priority 3: Vague Names (Context-Dependent)

Add prefix/suffix clarifying purpose:

```python
# Before
class Response(BaseModel):
    key: str
    id: str

# After
class Response(BaseModel):
    cache_key: str  # or request_hash, etc.
    response_id: str  # or request_id, depending on what it represents
```

### Priority 4: Local Variables (SHORT-LIVED) - MEDIUM

```python
# Before
req = build_request()
resp = fetch(req)

# After
request = build_request()
response = fetch(request)
```

---

## When Short/Vague Names Are Acceptable

### ✓ Momentary lifespan (1-3 lines)

```python
for i in range(n):        # OK: index
    for j in range(m):    # OK: nested index
        ...

for key, value in items:  # OK: momentary scope
    process(key, value)

items = [x for x in values if x > 0]  # OK: comprehension
```

### ✓ Strong container context

```python
class User(BaseModel):
    id: int        # OK: User.id is clear
    name: str      # OK: User.name is clear

class CacheEntry(BaseModel):
    key: str       # OK: CacheEntry.key is clear
```

### ✓ Module context provides clarity

```python
# File: kubernetes_client.py

# GOOD: Module name provides context, avoid overly long Java-style names
class Config(BaseModel):  # Clear it's KubernetesClientConfig from module context
    endpoint: str
    timeout: int

class Client:  # Clear it's KubernetesClient from module context
    def __init__(self, config: Config):
        ...

# BAD: Redundantly long when module already provides context
class KubernetesClientConfig(BaseModel):  # Redundant! File is already kubernetes_client.py
    ...

class KubernetesClient:  # Redundant!
    ...


# File: cache/redis.py

# GOOD: Short names when module provides context
class RedisCache:  # Or just Cache if module is cache/redis.py
    ...

# BAD: Overly specific when import already clarifies
from cache.redis import RedisDistributedInMemoryCache  # Redundant!
# vs.
from cache.redis import Cache  # Clear from import path
```

**Principle**: Leverage the import path and module name to avoid redundantly long class names. When you import `from kubernetes_client import Config`, the context is clear. Avoid Java-style `KubernetesClientConfigurationSettings` when `Config` suffices given the module context.

### ✓ Single entity of type in scope

```python
def process_task(task: Task, cfg: OptimizerConfig):  # OK: only one config
    cfg.validate()
```

### ✓ Standard conventions

```python
args, kwargs, cls, self  # Standard Python
df, pd, np               # Pandas/NumPy ecosystem
e, exc                   # Exception in short except blocks
```

---

## Benefits

✅ **Readability** - Code is self-documenting
✅ **Maintainability** - New contributors understand code faster
✅ **Searchability** - Full words easier to grep than abbreviations
✅ **IDE support** - Better autocomplete with full names
✅ **Consistency** - Follows Google Python Style Guide
✅ **Scalability** - Naming clarity grows with identifier importance

---

## Examples from Codebase

### Abbreviated → Descriptive

```python
# ✗ BEFORE: Abbreviated field (LONG-LIVED)
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._o = owner  # Cryptic!

# ✓ AFTER: Descriptive field
class _ChildHandler:
    def __init__(self, owner: Compositor, name: str) -> None:
        self._compositor = owner  # Clear!
```

### Generic → Specific

```python
# ✗ BEFORE: Vague in generic container
class Response(BaseModel):
    key: str  # What kind of key?

# ✓ AFTER: Specific name
class Response(BaseModel):
    cache_key: str  # SHA256 hash of request body
```

### Multiple Configs → Specific Names

```python
# ✗ BEFORE: Ambiguous which config
def process_rollout(rollout, task, cfg, grading_cfg, model_cfg):
    cfg.get_value()  # Which config?

# ✓ AFTER: Clear specificity
def process_rollout(rollout, task, optimizer_config, grading_config, model_config):
    optimizer_config.get_value()  # Clear!
```

---

## Recall/Precision Estimates

### Abbreviated Identifiers

- **Automated detection**: ~85-90% recall, ~70% precision
  - High recall: patterns are clear (length, common abbreviations)
  - Some false positives: acceptable conventions (i, j, k, df, pd)
- **Manual filtering needed**: Review ~30% to eliminate acceptable uses

### Vague Identifiers

- **Automated detection**: ~30-40% recall, ~20% precision
  - Low recall: context-dependent, semantic analysis required
  - High false positives: most `id` fields are actually fine
- **Manual review required**: Read model definitions and function signatures
  - Target: 100% recall through code reading

---

## References

- [Google Python Style Guide - Naming](https://google.github.io/styleguide/pyguide.html#s3.16-naming)
- [PEP 8 - Descriptive Naming Styles](https://peps.python.org/pep-0008/#descriptive-naming-styles)
- [Code Complete - Variable Naming Best Practices](https://www.oreilly.com/library/view/code-complete-2nd/0735619670/)
