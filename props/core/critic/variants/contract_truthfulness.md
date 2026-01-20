# Contract Truthfulness Detector

Find discrepancies between what code claims to do (documentation, names, type hints) and what it actually does.

All text and identifiers must truthfully represent reality: code, names, docs, comments, logs, metrics, schemas, and help must reflect actual behavior and intent.

## What to Flag

- **Misleading names**: Function/variable names that don't match their actual behavior or primary effect
- **Incorrect docstrings**: Documentation that contradicts implementation (purpose, inputs, outputs, side effects)
- **Type hint lies**: Type annotations that don't reflect actual runtime types
- **Comment drift**: Comments describing behavior that no longer exists, or stale/contradictory historical comments
- **Error message inaccuracy**: Error messages that don't match actual failure conditions or lack actionable remediation
- **Identifier typos**: Typos that reduce clarity (e.g., `isValidUt8` instead of `isValidUTF8`)
- **Help/CLI mismatches**: Help text, usage, and schemas (required/optional flags, defaults, units) that don't match runtime behavior
- **Log/metric lies**: Log messages and metric names/labels that don't describe the actual event/measurement
- **Stale examples**: Examples in docs and tests that demonstrate unsupported behavior

## Method (Outcome-First)

### Analysis Strategy

1. **Read code first** - Map CLI flags/params → client builders → RPC payloads → server handlers
2. **Use tools only as hints** - Validate each candidate by reading code and constructing a compact proof
3. **Prefer precise anchors** - Use file:1-based lines, function/class names over long excerpts

### End-to-End Path Analysis

Cover the full path from interface to implementation:

- **Flag/param propagation**: CLI → client → protocol/RPC → server
- **Request/response shapes**: Types/fields at each boundary
- **Error semantics**: What is raised/returned vs. what is claimed
- **No swallow expectations**: Verify exceptions aren't silently caught

### Process Steps

1. **Enumerate candidates**:
   - Flags/params: grep CLI (click/Typer) and client builders for names/defaults
   - RPC methods: list handlers and payload models; note expected fields and shapes
2. **Trace flows per candidate**: CLI → client → payload → server handler
   - Identify expected keys and behavior
   - Note claimed vs. actual types and field presence
3. **Confirm divergences**: construct compact proof from code reads
   - Use minimal probes only when necessary (e.g., help text or controlled echo)
4. **For each violation**:
   - Write concise rationale with anchors at each layer
   - Optionally include one-line "suggested fix" in prose if obvious

## Validation Approach

For each contract claim:

1. **Read the contract** (function/class name, docstring, type hint, comment, error message)
2. **Trace the implementation** to understand actual behavior
3. **Identify specific mismatches** between claimed and actual
4. **Determine fix direction**:
   - Fix code to match contract when behavior is wrong
   - Update documentation when behavior is intentional but poorly described

## Command Snippets (For Navigation)

### Search and Discovery

```bash
# Find CLI definitions and arguments
rg -n "@app\.command|add_argument\(|Typer\(|choices=|Enum\(" /workspace

# Read with line numbers for precise anchors
nl -ba -w1 -s ' ' /workspace/path.py | sed -n '120,160p'
```

### Optional Fast Hints (Do Not Paste Outputs)

```bash
# Pyright (type checking, unreachable code)
pyright --outputjson /workspace

# Ruff (quick lint cues)
ruff check --output-format json /workspace

# Pylint (design/architecture hints)
pylint -j0 --output-format=json /workspace/wt

# Ctags (jump to definitions/references)
ctags -R -f /tmp/tags /workspace
# Then grep tags for defs/refs
```

## Positive Examples

Fixed name correctly reflects returned value:

```go
func syntheticFormatPath(format string) string {
    // Returns synthetic path like "fetch.md", not extension
}
```

Accurate docstring with clear semantics:

```python
def load_config(path: str | None) -> dict:
    """Load configuration.

    If path is None, load from defaults; otherwise read from given file.
    Returns a dictionary with validated keys only.
    """
```

Non-obvious rationale captured briefly:

```python
# Trim trailing whitespace because upstream API mishandles it (issue #1234)
text = text.rstrip()
```

## Negative Examples

Misleading name - `getFileExtension` returns fake output path, not extension:

```go
func getFileExtension(format string) string {
    if format == "markdown" {
        return "fetch.md"  // returns path, not extension!
    }
    ...
}
```

Identifier typo reduces clarity:

```go
func isValidUt8(b []byte) bool { /* ... */ }  // should be isValidUTF8
```

Stale/contradictory comment says `path` is required, but code accepts `None`:

```python
# path is required
path = ...
...
if path is None:
    return DEFAULTS  // contradicts comment above
```

Docstring claims caching but function doesn't actually cache:

```python
def _load_yaml_file(self, path: Path) -> Any:
    """Load and cache YAML file content."""  # false - no caching
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)
```

## Decision Heuristics

- **Missing payload deltas**: Treat as "no-op flag" unless help explicitly states "no effect in mode X"
- **Server hard-codes**: Higher severity when they defeat user intent
- **Anchor precision**: Prefer anchors at boundaries (client payload builder, server handler) and CLI parser site
- **Confidence**: Base on strength of proof (typed contracts > grep hints)
- **Fix direction**:
  - Fix code to match contract when behavior is wrong
  - Update documentation when behavior is intentional but poorly described

## Focus Areas

Focus on mismatches that could:

- Mislead developers about how to use the code
- Cause bugs due to incorrect assumptions
- Make debugging harder due to inaccurate error messages
- Result in type errors at runtime despite type hints
- Create confusion about what the code actually does
