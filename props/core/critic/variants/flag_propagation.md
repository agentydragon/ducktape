# Flag Propagation & Control Flow Detector

Find issues where boolean flags or control flow states are unnecessarily threaded through code, and where control flow can be simplified.

## What to Flag

- **Redundant flag passing**: Flags passed down call chains but only checked at the end (consider early bailout or separate code paths)
- **Flag threading**: Boolean parameters that could be replaced by separate functions or early returns
- **Unnecessary state tracking**: Variables that track state already implicit in control flow (e.g., error flags set but checked later)
- **Complex flag combinations**: Multiple boolean flags where enum or strategy pattern would be clearer
- **One-off flag variables**: Flags assigned once and immediately passed to the next call without adding value
- **Nested trivial guards**: Multiple `if` statements that should be combined with `and`/`or` or use early bailout
- **Missing CLI flag propagation**: CLI flags that don't propagate to client/server or don't have the claimed effects

## Method (Outcome-First)

### Analysis Strategy

1. **Read CLI and handler code first** to understand flow
2. **Run minimal, surgical probes** only after understanding (e.g., head/tail of help text; small payload capture shims)
3. **Summarize deltas** - Never paste raw dumps into findings

### End-to-End Flag Assessment

For each flag/mode within scope (e.g., `--force`, `--dry-run`, `--timeout`):

- Does it propagate from CLI → client → protocol → server?
- Does it have the claimed effects?
- Target exhaustive coverage; if constrained, prioritize by impact

### Process Steps

1. **Enumerate candidate flags**:
   - Grep CLI definitions + help text
   - Order by impact for efficiency
2. **For each flag, trace the path**:
   - CLI → client → server (read code; use rg as needed)
   - Note expected payload keys/handlers
3. **Run minimal probe** to confirm behavior:
   - Capture only what's needed for rationale
   - Identify payload delta (expected vs. observed)
4. **Decide and document**:
   - Add finding with anchors at each layer
   - Write one-paragraph rationale

## Tracing Flag Usage

For each boolean parameter or state variable:

1. **Identify all occurrences** through call chains
2. **Trace usage patterns**:
   - Where is it checked/branched on?
   - How many layers deep before actual use?
   - Is it just forwarded through intermediate calls?
3. **Assess necessity**:
   - Does the flag add real branching logic?
   - Or does it just track state implicit in control flow?
4. **Consider refactoring options**:
   - Early returns to eliminate flag
   - Separate functions for different modes
   - Enum or strategy pattern for complex combinations

## Command Snippets (Examples)

### Search and Discovery

```bash
# Find flag definitions
rg -n "--force|--dry-run|--timeout" /workspace

# Read with line numbers for anchors
nl -ba -w1 -s ' ' /workspace/path/to/file.py | sed -n '120,160p'
```

### Optional Hints (No Raw Dumps in Final)

```bash
# Pyright (unreachable code hints)
pyright --outputjson /workspace

# Ruff (quick lint cues)
ruff check --output-format json /workspace

# Pylint (design/architecture smells)
pylint -j0 --output-format=json /workspace/wt

# Ctags (follow defs/refs while proving propagation paths)
ctags -R -f /tmp/tags /workspace
# Then grep tags to trace flag flow
```

### Payload Capture Shim (HTTP Example)

```python
# /tmp/shims/capture.py
import json, os, sys
from datetime import datetime
import requests

_orig = requests.Session.request

def _cap(self, method, url, **kw):
    body = kw.get('data') or kw.get('json')
    with open('/tmp/payloads.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps({
            'ts': datetime.utcnow().isoformat(),
            'method': method,
            'url': url,
            'body': body
        }) + '\n')
    return _orig(self, method, url, **kw)

requests.Session.request = _cap
```

Activate with: `PYTHONPATH=/tmp/shims <cli> <args>`

## Decision Heuristics

- **Missing payload deltas**: Treat as "no-op flag" unless help explicitly states "no effect in mode X"
- **Server hard-codes**: Higher severity when they defeat user intent
- **Anchor precision**: Prefer anchors at boundaries:
  - CLI parser site for context
  - Client payload builder for what's sent
  - Server handler for what's received/used

## Positive Examples

Early bailout instead of error flag:

```python
# Good: raise immediately when condition fails
def validate(item):
    if not item.valid:
        raise ValueError("invalid item")
    return process(item)
```

Combine trivial nested guards:

```python
# Good: single combined condition
if user and user.active and not user.error:
    grant_access(user)
```

Separate code paths instead of flag threading:

```python
# Good: two focused functions instead of one with a flag
def load_user_fast(uid: str) -> User:
    return cache.get(uid)

def load_user_full(uid: str) -> User:
    return db.get(uid)
```

## Negative Examples

Error flag deferred until later:

```python
# Bad: sets flag but checks later
ok = True
for part in parts:
    if part.invalid:
        ok = False
# ... many lines later ...
if not ok:
    raise ValueError("invalid part")
```

Nested trivial guards (should be combined):

```python
# Bad: trivial nesting
if user:
    if user.active:
        if user.team:
            if user.team.enabled:
                grant_access(user, user.team)
```

Flag threaded through multiple layers:

```python
# Bad: flag only checked at the end
def process(data, strict=False):
    result = parse(data, strict)
    return validate(result, strict)

def parse(data, strict):  # strict not used here
    return json.loads(data)

def validate(result, strict):  # finally used
    if strict and not result.valid:
        raise ValueError()
```

One-off flag variable:

```python
# Bad: flag assigned and immediately passed
enabled = feature_flags.get("new_ui")
return render_page(enabled)  # inline the call instead
```

## Focus Areas

Focus on cases where:

- Flag is passed through multiple layers but only checked at the end (early bailout or separate functions)
- Boolean parameter could be eliminated with separate code paths
- Multiple flags create complex combinations (enum or strategy pattern would be clearer)
- State variable tracks something already implicit in control flow (error flags, completion flags)
- Nested trivial guards should be combined with logical operators
- One-off variables don't add value (inline the expression)
- Removing or restructuring would make code clearer and less error-prone

## Notes on Application

- If target CLI requires a local daemon, document how to start/stop it offline and expected log/socket locations; otherwise skip daemon flows
- Provide sample fixtures/config if specific flows require inputs (no secrets)
