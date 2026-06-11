# Scan: Functions That Should Be Inlined

## Context

@../shared-context.md

## Pattern Description

Functions that exist purely to forward calls without reducing complexity. These should be inlined at their usage sites.

**Key principle**: Helper functions should exist IFF they make things LESS complex. If usage sites wouldn't be made longer or more complex by inlining the helper's body, the helper shouldn't exist.

## Examples: Should Inline

### BAD: Doesn't reduce complexity

```python
# BAD: Called exactly once, doesn't simplify the call site
def dump_response(value: OpenAIResponse | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json")

# Usage (called once):
result = dump_response(snapshot.response)

# BETTER: Inline it - usage site is no more complex
result = snapshot.response.model_dump(mode="json") if snapshot.response else None
```

```python
# BAD: Just forwarding to another module, adds no value
def extract_text_from_openai_response(response: ResponsesResult) -> str:
    return first_assistant_text(response)

# Usage:
text = extract_text_from_openai_response(response)

# BETTER: Inline it - just as clear
from module import first_assistant_text
text = first_assistant_text(response)
```

```python
# BAD: Short body, called twice, doesn't reduce complexity
def get_current_commit(repo: pygit2.Repository) -> pygit2.Oid:
    return repo.head.target

# Usage sites:
current = get_current_commit(repo)
parent = get_current_commit(other_repo)

# BETTER: Inline - usage sites are just as clear
current = repo.head.target
parent = other_repo.head.target
```

## Examples: Keep Function (Legitimate Reasons)

### GOOD: Facade pattern / API design

```python
# GOOD: Provides stable API even if implementation changes
class CacheClient:
    def get(self, key: str) -> dict[str, Any] | None:
        return self._backend.get(key)  # Facade over backend

    def set(self, key: str, value: dict[str, Any]) -> None:
        return self._backend.set(key, value)  # Facade over backend

# Reason: API stability, abstraction, dependency injection point
```

### GOOD: Implements interface or protocol

```python
# GOOD: Implements abstract method from base class
class SQLRepository(Repository):
    def save(self, item: Item) -> None:
        return self._session.add(item)  # Implementing interface

# Reason: Required by interface, even if body is simple
```

### GOOD: Actually reduces complexity

```python
# GOOD: Called 10+ times, consolidates complex pattern
def safe_json_loads(data: str | None) -> dict[str, Any]:
    if data is None:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON: %s", data)
        return {}

# Usage sites (10+ places):
config = safe_json_loads(row.config_json)
metadata = safe_json_loads(row.metadata_json)
...

# Reason: Consolidates error handling logic, used many times
```

### GOOD: Provides backward compatibility during migration

```python
# GOOD: Temporary during migration (document with TODO)
# TODO(2025-12): Remove after all callers migrated to new API
def get_user(user_id: str) -> User:
    return user_service.fetch_user(user_id)

# Reason: Temporary shim during refactoring (should be removed later)
```

## Detection Strategy

**MANDATORY Step 0**: Run single-line function body scanner.

- This scan is **required** - do not skip this step
- You **must** read and process ALL single-line function output using your intelligence
- High recall required, high precision NOT required - you determine which should be inlined
- Review each for: call count, architectural role, complexity reduction, legitimate reasons
- Prevents lazy analysis by forcing examination of ALL trivial function candidates

**Tool**: `prompts/scans/scan_single_line_functions.py` - AST-based scanner for single-line function bodies

**What it finds**:

- All functions (sync and async) with exactly ONE line of real code (excluding docstrings)
- Includes: function name, file, line number, the single statement, decorators, signature
- Groups by file for easier review

**Usage**:

```bash
# Run on entire codebase
python prompts/scans/scan_single_line_functions.py . > single_line_functions.json

# Pretty-print summary
cat single_line_functions.json | jq '.summary'

# View all single-line functions
cat single_line_functions.json | jq '.functions[] | {name, file, line, statement}'

# View by file
cat single_line_functions.json | jq '.by_file'
```

**What to review for each function:**

1. **Call count**: How many times is it called? (use grep, AST analysis)
2. **Architectural role**: Is this a facade, interface implementation, or API boundary?
3. **Complexity**: Would inlining make call sites more OR less complex?
4. **Legitimate reasons**: Protocol implementation, decorator target, backward compatibility
5. **Decision**: Inline if doesn't reduce complexity, keep if legitimate architectural reason

**Low precision expected (~20-30%)**: Many single-line functions are legitimate (facades, interface implementations, API boundaries). Agent must use judgment to distinguish trivial forwarders from legitimate simple functions.

---

**Goal**: Find ALL functions that should be inlined (100% recall target).

**Recall/Precision**: High recall (~90%) with automation, low precision (~20-30%)

- Functions called exactly once: ~95% recall, ~20% precision (many legitimate one-time wrappers)
- Functions with short body (<= 3 lines): ~85% recall, ~25% precision (many valid simple functions)
- Single-return functions: ~90% recall, ~30% precision (facades, interface implementations, etc.)

**Why low precision is expected**:

- Legitimate reasons for simple forwarders: facades, interfaces, API stability, backward compatibility
- Can't tell from syntax alone whether function reduces complexity at call sites
- Need to understand: call count, architectural role, complexity trade-offs

**Recommended approach AFTER Step 0**:

1. Process single-line function scanner output (MANDATORY Step 0)
2. For each candidate, analyze:
   - **Call count**: How many times is it called? (vulture, grep, AST)
   - **Complexity**: Would inlining make call sites more complex?
   - **Architecture**: Is this a facade, interface implementation, or API boundary?
   - **Purpose**: Does it consolidate logic or just shuffle calls?
3. Filter out legitimate forwarders (facades, interfaces, backward compat)
4. Inline confirmed trivial forwarders
5. **Supplement with manual reading** to find complex cases automation misses

**High-recall, low-precision retrievers AFTER Step 0**:

### 1. Functions Called Exactly Once (Especially Same File)

```bash
# Build call count database using AST or grep
# For each function, count references:
# - 1 reference (definition only) → unused (different pattern)
# - 2 references (definition + 1 call) → candidate for inlining
# - Especially if call is in same file

# Grep-based approximation (counts occurrences):
rg --type py "function_name" --count-matches

# Better: Use AST tool to build call graph, flag functions with exactly 1 caller
```

**High precision indicator**: Called once in same file → very likely should be inlined

### 2. Functions with Short, Low-Complexity Body

```bash
# Find functions with <= 3 lines (excluding docstrings)
# AST tool can count statements:
# - 1 statement (single return) → high candidate
# - 2-3 statements (simple logic) → medium candidate
# - No loops, no complex conditionals → higher candidate

# Grep approximation (single-return functions):
rg --type py -U "def \w+\([^)]*\):[^\n]*\n\s+return "

# Find very short functions (likely simple):
rg --type py -A3 "^def " | grep -B1 -A3 "return" | grep -B4 "^--$"
```

### 3. Single-Statement Return Functions

```bash
# Functions with body: single return statement
rg --type py -U "def \w+\([^)]*\):[^\n]*\n\s+return \w+\("
```

### 4. AST-Based Discovery (Comprehensive)

Build tool that analyzes:

```python
# Pseudocode for AST-based detection
for func in all_functions:
    if func.body_lines <= 3:
        call_count = count_calls_to(func.name)
        if call_count == 1:
            yield HighPriorityCandidate(func, reason="called once")
        elif call_count <= 3 and is_simple_body(func):
            yield MediumPriorityCandidate(func, reason="few calls, simple body")

        # Check if it's just forwarding
        if is_single_return_call(func):
            yield Candidate(func, reason="single return call")
```

**Verification for each candidate**:

1. **Check call count**: Functions called 1-2 times are high priority candidates
   - **CRITICAL**: Check if method is actually called from external code, not just by other methods in same class
   - Example: `close()` might only be called by `__aexit__()` internally, but that's protocol conformance

2. **Check interface/protocol conformance** (MOST IMPORTANT - prevents false positives):
   - **Python protocols**: Check if method is called by dunder methods:

     ```python
     # Context manager protocol
     __enter__() / __exit__() / __aenter__() / __aexit__()
     # Iterator protocol
     __iter__() / __next__()
     # Sequence protocol
     __len__() / __getitem__() / __setitem__()
     ```

   - **Abstract base classes**: Check if method overrides ABC abstract method:

     ```bash
     # Look for class inheritance from ABC
     rg "class.*\(.*ABC.*\)" file.py
     # Look for @abstractmethod in parent class
     ```

   - **Framework interfaces**: Check for standard API patterns:

     ```python
     # Gym environment: reset(), step(), render(), close()
     # Django models: save(), delete(), clean()
     # Context managers: __enter__(), __exit__(), close()
     # FastAPI dependencies: __call__()
     ```

   - **If method is required by protocol/interface → KEEP** (even if trivial forwarder)

3. **Check architectural role**:
   - Method overriding abstract method? → Keep (interface requirement)
   - Public method in facade class AND actually called externally? → Keep (API design)
   - Public method in facade class but NO external callers? → Consider removing
   - Private helper with simple body? → Likely inline

4. **Complexity analysis**:

   ```python
   # For each call site:
   # Current: result = helper_func(arg1, arg2)
   # After inline: result = <helper_body with arg1, arg2>
   # Is "After" significantly longer or more complex? If no → inline
   ```

5. **Check for consolidation**:
   - Does function consolidate error handling? → Keep
   - Does function consolidate validation? → Keep
   - Does function just forward? → Inline (unless protocol/interface conformance)

## Decision Framework: Inline or Keep?

For each candidate function, ask:

### 1. **Call Count Test**

- Called once in same file? → **Strong inline candidate**
- Called 2-3 times with simple body? → **Medium inline candidate**
- Called 10+ times? → **Check complexity benefit**

### 2. **Complexity Test**

```python
# Simulate inlining at each call site:
# Would this make the call site:
# - Longer? (By how much? 1 line → 3 lines might be fine)
# - More complex? (Nested conditionals, error handling)
# - Less clear? (Complex expression vs named function)

# If call sites become more complex → KEEP function
# If call sites stay same complexity → INLINE
```

### 3. **Architectural Role Test**

**Check in this order** (most common to least common):

1. [ ] **Protocol/interface conformance?** → **KEEP** (highest priority check)
   - Called by dunder methods? (`__aexit__`, `__enter__`, `__iter__`, etc.)
   - Part of framework interface? (Gym, Django, FastAPI, etc.)
   - Overrides abstract method from ABC?
2. [ ] **Public API actually used externally?** → **KEEP**
   - Check if method is called from outside the class
   - Facade with actual external callers
3. [ ] **Backward compatibility shim?** → **KEEP (temporarily)**
   - Document with TODO if temporary
4. [ ] **Dependency injection point?** → **KEEP**
   - Provides customization for testing
5. [ ] **Private helper, simple body?** → **LIKELY INLINE**

### 4. **Consolidation Test**

- Consolidates error handling? → **KEEP**
- Consolidates validation logic? → **KEEP**
- Consolidates complex computation? → **KEEP**
- Just forwards calls? → **INLINE**

## Fix Strategy (When Inlining)

1. **Identify all call sites**:

   ```bash
   rg --type py "function_name\("
   ```

2. **Inline the function body** at each call site:

   ```python
   # Before:
   result = helper_func(arg1, arg2)

   # After (inline function body):
   result = <body of helper_func with arg1, arg2 substituted>
   ```

3. **Remove function definition** after all call sites updated

4. **Update imports** if needed (if helper imported underlying function)

5. **Verify**: Run mypy and tests

## When to Keep (Don't Inline)

These patterns have **legitimate reasons** for simple forwarding:

### Protocol/Interface Conformance (HIGHEST PRIORITY - Most Common False Positives)

**Python Protocols**:

- **Context managers**: `close()` called by `__aexit__()`, even if trivial
- **Iterators**: `__next__()` forwarding to internal iterator
- **Async protocols**: `__aenter__()` / `__aexit__()` / `close()` / `stop()`
- **Descriptors**: `__get__()` / `__set__()` / `__delete__()`

**Framework Interfaces**:

- **Gym environments**: `reset()`, `step()`, `render()`, `close()` - standard API
- **Django**: Template tags must be functions (can't call constructors directly)
- **FastAPI**: Dependencies implementing `__call__()`
- **SQLAlchemy**: Repository pattern methods implementing interface

**How to verify**:

```bash
# Check if method is called by dunder methods
rg "def __(enter|exit|aenter|aexit|iter|next)" file.py -A10 | grep "method_name"

# Check class inheritance for ABC or known frameworks
rg "class.*\((ABC|gym\.Env|BaseModel|Repository)" file.py

# Check if it's a public API actually used externally
rg "from.*import.*ClassName" --type py | wc -l  # Check usage count
```

### Architectural Patterns

- **Facade pattern**: Stable API over changing implementation
  - **IMPORTANT**: Only keep if actually used externally (check call sites)
  - Example: SearchService was removed because only 2 of 5 methods used by single caller
- **Public API with external callers**: Method is part of class's public interface AND called from outside
- **Dependency injection**: Provides customization point for testing

### Complexity Reduction

- **Consolidates error handling**: Multiple try/except blocks → single function
- **Consolidates validation**: Complex checks used in multiple places
- **Called many times**: 10+ call sites benefit from centralized logic
- **Semantic clarity**: Wrapper name significantly clearer than underlying call
  - Example: `get_split_amount(split)` vs `gnc_numeric_to_python_Decimal(split.GetAmount())`

### Temporary Patterns

- **Backward compatibility**: During migration/refactoring (document with TODO)
- **API versioning**: Supporting old API during deprecation period

## Complete Example: Inlining Decision

### Candidate: `dump_response`

```python
# Function definition:
def dump_response(value: OpenAIResponse | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json")

# Called exactly once:
result = dump_response(snapshot.response)
```

**Decision analysis**:

1. ✅ **Call count**: Called once → strong inline candidate
2. ✅ **Complexity test**:
   - Current: 1 line
   - After inline: 1 line (ternary expression)
   - No increase in complexity
3. ✅ **Architectural role**: Private helper, not interface/facade
4. ✅ **Consolidation test**: Just forwards, no error handling

**Decision**: **INLINE**

**Fix**:

```python
# Before:
from module import dump_response
result = dump_response(snapshot.response)

# After:
result = snapshot.response.model_dump(mode="json") if snapshot.response else None
```

### Counter-Example: `safe_json_loads` (Keep)

```python
# Function definition:
def safe_json_loads(data: str | None) -> dict[str, Any]:
    if data is None:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON: %s", data)
        return {}

# Called 15 times across codebase:
config = safe_json_loads(row.config_json)
metadata = safe_json_loads(row.metadata_json)
...
```

**Decision analysis**:

1. ❌ **Call count**: Called 15 times → check complexity benefit
2. ❌ **Complexity test**:
   - Current: 1 line per call site
   - After inline: 6 lines per call site (try/except block)
   - Significant increase in complexity (×15)
3. ❌ **Consolidation test**: Consolidates error handling logic

**Decision**: **KEEP** - Reduces complexity by consolidating error handling

---

### Counter-Example: Context Manager Protocol (Keep)

```python
# Class with async context manager protocol
class MatrixClient:
    async def stop(self) -> None:
        # ... cleanup logic ...
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def close(self) -> None:
        await self.stop()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
```

**Why flagged:** `close()` is a trivial forwarder to `stop()`

**Decision analysis**:

1. ❌ **Call count**: Only called once (by `__aexit__`)
2. ❌ **Interface conformance**: **Protocol requirement**
   - `__aexit__()` calls `close()` - this is async context manager protocol
   - Provides semantic clarity: `close()` for context managers, `stop()` for explicit cleanup
3. ❌ **Architectural role**: Part of Python async context manager protocol

**Decision**: **KEEP** - Required for protocol conformance, even though trivial

---

### Counter-Example: Framework Interface (Keep)

```python
# Gym environment wrapper
class OpaqueEnvironmentWrapper:
    def __init__(self, env_name: str):
        self.env = gym.make(env_name)

    def reset(self):
        obs = self.env.reset()
        return self._flatten_observation(obs)

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        return self._flatten_observation(obs), reward, done, info

    def close(self):
        self.env.close()  # Trivial forwarder!
```

**Why flagged:** `close()` just forwards to `self.env.close()`

**Decision analysis**:

1. ❌ **Interface conformance**: **Framework requirement**
   - Gym environments are expected to have `reset()`, `step()`, `render()`, `close()`
   - Even if `close()` just forwards, it's part of the standard Gym interface
   - Callers expect to call `wrapper.close()`, not `wrapper.env.close()`
2. ❌ **Architectural role**: Wrapper implementing standard interface

**Decision**: **KEEP** - Required by Gym environment interface

---

### Counter-Example: Method Actually Used by Public API (Keep)

```python
class SettingsWrapper:
    def __init__(self, schema: str, path: str):
        self.settings = Gio.Settings.new_with_path(schema, path)

    def sync(self) -> None:
        self.settings.sync()  # Trivial forwarder!

# Usage - called 3 times by external code:
profile_list = ProfileList()  # subclass of SettingsWrapper
profile_list["default"] = uuid
profile_list.sync()  # Called from external code

profile = Profile(uuid)  # subclass of SettingsWrapper
profile.apply_color_scheme(...)
profile.sync()  # Called from external code
```

**Why flagged:** `sync()` just forwards to `self.settings.sync()`

**Decision analysis**:

1. ❌ **Call count**: Called 3 times by external code (not just internal methods)
2. ❌ **Architectural role**: Part of public API, actually used by callers
3. ❌ **Facade pattern**: Wrapper provides public API boundary

**Decision**: **KEEP** - Part of public API with actual external callers

**Lesson**: Just because a method forwards doesn't mean it's unused. Check actual call sites!

---

## Scan Results Structure

**IMPORTANT**: The scan results MUST include reasoning and categorization, not just a list of candidates.

For each finding, apply the Decision Framework and categorize:

### ✅ Should Inline (True Positives)

- **What**: Function name, location, evidence
- **Why it should be inlined**: Apply Decision Framework
  - Call count test result
  - Complexity test result
  - Architectural role test result
  - Consolidation test result
- **Recommended action**: Specific fix (show before/after)

### ❌ Should Keep (False Positives / Justified Forwarders)

- **What**: Function name, location
- **Why it should be kept**: Specific reason from "When to Keep" section
  - Facade/interface implementation
  - Consolidates error handling/validation
  - Called many times with complexity benefit
  - Framework requirement (e.g., Django template tags)
  - Backward compatibility shim
- **Decision**: No action needed (justified)

### Example Result Format

```markdown
## Findings

### True Positives (Should Inline)

#### 1. SearchService Facade Methods

**File:** `/path/to/search.py`
**Lines:** 19-32

**Evidence:** 5 methods that just forward to module functions

- `get_node()` → forwards to `_graph[node_id]`
- `materialize()` → forwards to `materialize_search()`
- etc.

**Decision Framework Analysis:**

1. ✅ **Call count**: Only 2 of 5 methods used, both called from single caller (Workspace class)
2. ✅ **Complexity test**: Inlining doesn't increase complexity (1 line → 1 line)
3. ✅ **Architectural role**: Not implementing interface, not public API boundary
4. ✅ **Consolidation test**: No error handling or validation added

**Decision**: **INLINE** - Remove SearchService, use direct imports in Workspace

**Recommended fix:**

- Replace `SearchService(graph)` with direct imports
- Update Workspace methods to call functions directly
- Delete SearchService class

---

### False Positives (Keep - Justified)

#### 1. Django Template Tag Wrapper

**File:** `/path/to/custom_tags.py`
**Line:** 233-234

**Why flagged:** Single-line function forwarding to constructor

**Why it should be kept:** **Framework requirement**

- Django's `@register.simple_tag` decorator requires function signature
- Template engine calls functions, not constructors directly
- Not a code smell - necessary for framework integration

**Decision**: No action needed (justified forwarder)

---

#### 2. GnuCash Utility Wrapper

**File:** `/path/to/gnucash_util.py`
**Line:** 35-36

**Why flagged:** Single-line wrapper around conversion function

**Why it should be kept:** **Semantic clarity**

- Called 3 times in reconciliation code
- `get_split_amount(split)` clearer than `gnc_numeric_to_python_Decimal(split.GetAmount())`
- One usage as key function for sorting - shorter name improves readability
- Abstracts GnuCash's awkward numeric type conversion

**Decision**: No action needed (readability benefit justifies wrapper)
```

## Validation

```bash
# After inlining, verify no references remain
rg "function_name\("

# Run type checker
mypy path/to/modified/files.py

# Run tests to ensure behavior unchanged
pytest path/to/tests/
```
