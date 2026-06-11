# Dead Code & Unreachability Detector

Find dead code: unused symbols, unreachable branches, and test-only production code.

## What to Flag

- **Unused symbols**: Functions, classes, methods, variables, constants not referenced from production code
- **Unreachable branches**: Match/case arms, if/elif branches ruled out by invariants or types
- **Redundant guards**: Tautologies, contradictions, mutually exclusive checks, trivially nested conditions
- **Test-only production code**: Code only invoked from tests (unless clearly marked as test-only)
- **Dead switches/if-chains**: Arms for states that cannot occur given the function's contract
- **Stale feature flags**: Compatibility shims no longer referenced or active

## Method (Outcome-First)

### Analysis Strategy

1. **Read code and build mental model first** - Identify entrypoints and plausible flows before consulting tools
2. **Use tools only as spotters/hints** - When a tool suggests a candidate, validate by reading code and constructing a proof
3. **Prefer precise anchors** - Use function/class names and 1-based line ranges over long code dumps

### Process Steps

1. **Inventory exported/public entrypoints** (CLI/HTTP/API) to focus reachability checks; note high-risk modules
2. **Dead code scan** (reasoning-first):
   - Build inbound reference sets for top-level symbols (rg search + confirmation by reading call sites)
   - Classify dead/test-only vs internal-only
   - For dead items, add a one-line proof
   - Optionally consult Vulture to spot misses; validate every candidate by hand
3. **Unreachable/missing arms**:
   - For choices/Enums vs match/if-elif arms, flag missing/unreachable cases
   - Include anchors (choices def + arm lines) and a sentence explaining why
4. **Redundant/subsumed guards**:
   - Within a function, flag tautologies, contradictions, and trivial nested guards
   - Cite the parent guard and the redundant site

## Proof Construction

For each finding, include a compact proof:

- **Dead code**: "No inbound references from exported call sites"
- **Unreachable arms**: "Argparse choices exclude 'turbo' so arm is unreachable"
- **Redundant guards**: Cite parent guard that makes nested check tautological

When building reachability proofs:

- Either collect a compact chain: entrypoint → … → branch (files:lines)
- Or state "no path; unreachable"
- Optionally write `/tmp/reachability_proofs.md` grouping by module/function

## Command Snippets

### Baseline Static Analysis (Discrete)

```bash
# Ruff (linting hints)
ruff check --output-format json /workspace > /tmp/ruff.json

# Mypy (type checking, dead code hints)
mypy --hide-error-context --show-error-codes /workspace > /tmp/mypy.txt
# Or with strict mode: mypy --strict /workspace

# Vulture (dead code detection)
vulture /workspace --min-confidence 60 --sort-by-size > /tmp/vulture.txt
# Or JSON if available

# Custom detectors
adgn-detectors-custom --root /workspace --out /tmp/custom-findings.json
```

### Code Navigation

```bash
# Find choices/Enums
rg -n "choices=\[|choices=\(|Enum\(" /workspace

# Find match/case statements
rg -n "^\s*match\s|^\s*case\s" /workspace

# Find isinstance chains
rg -n "isinstance\(.*?,\s*\(" /workspace

# Read specific line ranges with numbers
nl -ba -w1 -s ' ' /workspace/path/to/file.py | sed -n '120,180p'
```

### Optional Additional Tools (Do Not Paste Raw Output)

```bash
# Pyright (fast unreachable code hints)
pyright --outputjson /workspace  # reportUnreachableCode

# Bandit (security smells)
bandit -q -r /workspace -f json

# Radon/Xenon (complexity hotspots)
radon cc -s -j /workspace
xenon --max-absolute A --max-modules B --max-average B /workspace

# Pylint (design/architecture hints)
pylint -j0 --output-format=json /workspace/wt

# Ctags (build reference index)
ctags -R -f /tmp/tags /workspace
# Then grep tags for defs/refs to build proofs

# JSCPD (duplication hotspots)
jscpd --path /workspace --reporters json

# Import linter (boundary violations)
lint-imports -c importlinter.cfg
# Agent may scaffold minimal config if absent

# Pyan (call graph hints)
pyan /workspace/**/*.py --uses --no-defines -o /tmp/callgraph.dot
```

## Exceptions

The following cases are NOT dead code:

- **Plugin hooks/interfaces**: Dynamically resolved from registries, entry-points, or configuration. Must have a comment proving reachability:

  ```python
  def plugin_has_no_references_in_python():
      """Plugin, dynamically resolved from configuration YAML."""
      ...
  ```

- **Temporary compatibility shims**: May remain during migration with an owner and removal date

## Positive Examples

Hard guard instead of fallback (acceptable sentinel):

```python
match mode:
    case "fast":
        run_fast()
    case "slow":
        run_slow()
    case _:
        raise AssertionError(f"unreachable mode: {mode!r}")
```

Type-driven branches remove impossible cases:

```python
def handle(x: Bar | Baz | Quux) -> str:
    if isinstance(x, Bar):
        ...
    elif isinstance(x, Baz):
        ...
    elif isinstance(x, Quux):
        ...
    else:
        # Unreachable; keep at most a hard guard
        raise TypeError(f"Unexpected {type(x) = }")
```

Test-only function clearly marked:

```go
func MakeTestSession() *Session { ... }  // OK
```

## Negative Examples

Unreachable branch ruled out by type signature:

```python
def get_user(uid: uuid.UUID) -> User:
    if not uid:
        return None  # impossible given function contract - delete
    return db.load_user(uid)
```

Redundant check ruled out by earlier guard:

```go
if basePath == "" {
    full := matchesGlob(pattern, path)
    base := matchesGlob(pattern, filepath.Base(path))
    return full || base
}
...
if !validFile || basePath == "" {  // remove basePath check - ruled out above
    return false
}
```

Speculative fallback that cannot happen:

```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["fast", "slow"])
# ... later in code
if mode not in {"fast", "slow"}:
    # unreachable: CLI parser restricts choices; delete branch
    return default()
```

Unused symbol kept "just in case":

```python
DEFAULT_TIMEOUT_SECONDS = 30  # not referenced anywhere - remove
```

## Heuristics & Confidence

- **Prefer typed proofs** (Mypy + Enums/choices) over rg-only hints; annotate confidence accordingly
- **Treat registry/plugin interfaces as dynamic entrypoints** - Avoid false positives unless registry scan proves otherwise
- **Validation required** - Every tool suggestion must be validated by reading code
- **Use local reasoning**: If a branch is obviously unreachable given established invariants, delete it
- **Strengthen invariants**: Prefer strengthening validations over keeping speculative fallback branches

## Notes on Application

- Identify target app CLI entrypoints (console scripts or modules) and any subpackages to exclude from analysis
- If HTTP route decorations are non-standard, provide route patterns; otherwise index FastAPI/Flask decorators by default
- Provide exclude globs for generated/vendor directories to reduce noise
