# Scan: Legacy Backward Compatibility Aliases

## Context

@../shared-context.md

## Pattern Description

Backward compatibility aliases that create duplicate names for the same entities. These include:

- TypeScript/JavaScript export aliases (`export const oldName = newName`)
- Python import aliases for backward compatibility
- Constants assigned to other constants (`OLD_NAME = NEW_NAME`)
- Trivial forwarder functions (see also: `trivial-forwarders.md`)
- Comments/strings indicating temporary backward compatibility

**Key principle**: Aliases should be removed by squashing to the canonical name. Temporary backward compatibility is acceptable during migration but should be cleaned up, not left indefinitely.

## Examples

### TypeScript Export Aliases

#### BAD: Backward compatibility export aliases

```typescript
// Backward compatibility: function name aliases
export const connectAgentWs = connectAgentChannels;
export const disconnectAgentWs = disconnectAgentChannels;

// Old constant names kept for backward compat
export const AGENT_WS_ENDPOINT = AGENT_CHANNELS_ENDPOINT;
export const WS_RECONNECT_DELAY = CHANNEL_RECONNECT_DELAY;
```

#### GOOD: Single canonical export

```typescript
// Single canonical name, no aliases
export const connectAgentChannels = ...
export const disconnectAgentChannels = ...

export const AGENT_CHANNELS_ENDPOINT = ...
export const CHANNEL_RECONNECT_DELAY = ...
```

### Python Import Aliases

#### BAD: Backward compatibility import aliases

```python
# Old module location kept for backward compat
from new_location import NewClass as OldClass
from refactored.module import new_function as old_function

# Re-export with old name
from .current import Thing
OldThing = Thing  # Backward compat
```

#### GOOD: Direct imports from canonical location

```python
from new_location import NewClass
from refactored.module import new_function

from .current import Thing
```

### Python Constant Aliases

#### BAD: Constants aliasing other constants

```python
# Backward compatibility constant aliases
LEGACY_API_URL = NEW_API_URL
OLD_TIMEOUT = DEFAULT_TIMEOUT
DEPRECATED_MAX_RETRIES = MAX_RETRIES

# Type aliases for old names
OldType = NewType
LegacyModel = CurrentModel
```

#### GOOD: Single canonical constant

```python
# Single canonical name
NEW_API_URL = "https://api.example.com"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3

# Single canonical type
NewType = TypedDict("NewType", {...})
CurrentModel = ...
```

### Python Property Aliases

#### BAD: Properties that just return other attributes

```python
class Config:
    def __init__(self):
        self.new_value = "foo"

    @property
    def old_value(self) -> str:
        """Deprecated: use new_value"""
        return self.new_value  # Backward compat alias
```

#### GOOD: Single attribute name

```python
class Config:
    def __init__(self):
        self.new_value = "foo"
```

### Comments Indicating Backward Compatibility

#### Detection Patterns

```python
# Comments that indicate temporary backward compatibility:
# "Backward compatibility"
# "Backward compat"
# "Legacy alias"
# "Deprecated: use X instead"
# "TODO: Remove after migration"
# "Keep for backward compatibility"
# "Old name for compatibility"

# Example:
OLD_SETTING = NEW_SETTING  # Backward compatibility - remove after v2.0
```

These comments are strong signals that the code should be cleaned up by:

1. Finding all usages of the old name
2. Replacing with canonical name
3. Removing the alias

## Detection Strategy

**Goal**: Find ALL backward compatibility aliases (100% recall target).

**Recall/Precision**: High recall (~85-95%) with automation, low-medium precision (~40-60%)

**Why medium precision is expected**:

- Some aliases are legitimate (facade pattern, API stability)
- Need context to determine if alias is truly temporary backward compat vs architectural pattern
- Comments about "backward compatibility" are strong signal but need verification
- Some trivial forwarders are justified (see `trivial-forwarders.md` for comprehensive decision framework)

### Primary Detection Methods

#### 1. TypeScript Export Aliases (High Precision ~70%)

**Pattern**: `export const X = Y` where Y is not a function call/object literal

```bash
# Find export aliases (variable assigned to another variable)
rg --type ts --type tsx 'export const \w+ = \w+$'

# Find with backward compat comments nearby
rg --type ts --type tsx -B2 -A2 'backward.compat|legacy'
```

**High precision indicators**:

- Comment mentions "backward compat", "legacy", "deprecated"
- Variable name contains "old", "legacy", "deprecated"
- Both names exist in same file (clear aliasing)

**Verification needed**:

- Check if RHS is actually another identifier (not a function call)
- Verify both names refer to same entity
- Check if this is genuinely temporary or architectural pattern

#### 2. Python Import Aliases (Medium Precision ~50%)

**Pattern**: `from X import Y as Z` where Z suggests backward compatibility

```bash
# Find import aliases with old/legacy/deprecated in name
rg --type py 'from .* import \w+ as (\w*[Oo]ld\w*|\w*[Ll]egacy\w*|\w*[Dd]eprecated\w*)'

# Find import aliases near backward compat comments
rg --type py -B2 -A2 'backward.compat|legacy|deprecated.*use' | grep -A2 -B2 'import.*as'
```

**High precision indicators**:

- Alias name contains "old", "legacy", "deprecated"
- Comment nearby explains backward compatibility
- TODO comment about removing the alias

**Verification needed**:

- Check if alias is used anywhere (might be dead code)
- Verify comment context (is this temporary or permanent?)
- Check if removing would break external API

#### 3. Python Constant Aliases (High Precision ~65%)

**Pattern**: Assignment where RHS is a simple identifier (not a function call)

```bash
# Find simple constant aliases (NAME = OTHER_NAME)
rg --type py '^[A-Z_]+ = [A-Z_]+$'

# Find constant aliases with backward compat comments
rg --type py -B2 'backward|legacy|deprecated|old.*name' | grep -A1 '^[A-Z_]+ = [A-Z_]+'

# Find TypeAlias assignments to other types
rg --type py '^\w+ = \w+\s*#.*(?:backward|legacy|deprecated|alias)'
```

**High precision indicators**:

- Comment explicitly mentions backward compatibility
- Variable names suggest old→new relationship (OLD_X = NEW_X)
- Both constants defined in same module

**False positives to filter**:

- Semantic aliases for clarity (`MAX_RETRIES = DEFAULT_RETRIES` might be intentional)
- Re-exports for public API (`__all__` driven exports)

#### 4. Python Property Aliases (Medium Precision ~55%)

**Pattern**: Property that just returns another attribute

```bash
# Find properties with single return statement
rg --type py -U '@property\s+def \w+\([^)]*\)[^:]*:\s+"""[^"]*deprecated[^"]*"""\s+return self\.\w+'

# Find properties near deprecation comments
rg --type py -B1 '@property' | grep -A5 'deprecated\|backward\|legacy'
```

**Verification needed**:

- Check if property adds any logic (validation, transformation)
- Verify it's truly just forwarding to another attribute
- See `trivial-forwarder-methods.md` for comprehensive property evaluation

#### 5. Backward Compatibility Comments (High Recall ~90%, Medium Precision ~50%)

**Pattern**: Comments indicating temporary backward compatibility

```bash
# Find backward compatibility comments
rg --type py --type ts --type tsx -i '(backward.compat|legacy|deprecated.*use|old.*name|TODO.*remove.*compat)'

# Find comments with specific deprecation language
rg --type py --type ts -i 'deprecated:? use \w+|old name for'

# Find TODO comments about removing backward compat
rg --type py --type ts 'TODO.*remove.*(after|compat|migration|legacy)'
```

**High precision indicators**:

- "Deprecated: use X instead" → clear guidance to migrate
- "TODO: Remove after [date/version]" → temporary, should be cleaned up
- "Backward compatibility" on assignment line → very likely alias

**Lower precision cases**:

- Generic "TODO: cleanup" without specifics
- Comments about maintaining compatibility (might be permanent API)

#### 6. Trivial Forwarder Functions

**See**: `trivial-forwarders.md` for comprehensive detection strategy, decision framework, and examples.

**Quick patterns**:

```bash
# Python: Single-line return forwarding to another function
rg --type py -U "def \w+\([^)]*\):[^\n]*\n\s+return \w+\("

# TypeScript: Arrow function forwarding
rg --type ts --type tsx 'export const \w+ = \([^)]*\) => \w+\('

# Functions called exactly once (strong inline candidate)
# Requires AST analysis or call graph tool
```

**Note**: Function-level forwarders are covered comprehensively in `trivial-forwarders.md`. This scan focuses on non-function aliases (constants, imports, exports, properties).

### Recommended Workflow

1. **Run high-recall retrievers** to gather ALL candidates:
   - TypeScript export aliases
   - Python import aliases
   - Python constant aliases
   - Python property aliases
   - Backward compatibility comments
   - (Function forwarders covered by `trivial-forwarders.md`)

2. **For each candidate, analyze**:
   - **Usage count**: Is the old name still used? (grep/ripgrep)
   - **Comment context**: Is there explicit backward compat documentation?
   - **Architectural role**: Is this a temporary shim or permanent API design?
   - **External API**: Would removing break external callers?

3. **Categorize findings**:
   - **High confidence removals**: TODO comments, explicit "deprecated", low usage
   - **Medium confidence**: Backward compat comments, naming suggests old→new
   - **Keep for now**: No clear signal, might be architectural, high external usage

4. **Fix strategy** (see below)

5. **Supplement with manual reading** to find:
   - Complex aliasing patterns automation missed
   - Architectural context that changes decision
   - Related aliases that should be cleaned up together

## Fix Strategy: Squashing to Canonical Name

**Principle**: Remove aliases by migrating all usages to the canonical name.

### Step 1: Identify Canonical Name

For each alias pair, determine which is canonical:

```python
# Example: Which to keep?
OLD_CONFIG = NEW_CONFIG  # Comment says "use NEW_CONFIG"

# Canonical: NEW_CONFIG (comment explicitly directs to it)
# Remove: OLD_CONFIG
```

**Heuristics for canonical name**:

1. Comment explicitly says "use X instead" → X is canonical
2. `new_*` vs `old_*` → new is canonical
3. More descriptive name → canonical
4. Name without "legacy"/"deprecated" prefix → canonical
5. If unclear, pick one consistently and document in commit message

### Step 2: Find All Usages of Old Name

```bash
# Find all usages of old name
rg --type py --type ts --type tsx '\bOLD_NAME\b'

# Count usages (helps prioritize)
rg --type py '\bOLD_NAME\b' --count-matches
```

**Important**: Check for usages in:

- Source code (imports, references)
- Tests (might use old name)
- Documentation (might reference old name)
- Type definitions (TypeScript .d.ts files)
- External packages that import from this codebase (if public API)

### Step 3: Replace Old Name with Canonical Name

#### TypeScript/JavaScript

```typescript
// Before:
export const connectAgentWs = connectAgentChannels;
export const disconnectAgentWs = disconnectAgentChannels;

// All call sites using old names:
connectAgentWs(agentId);
disconnectAgentWs(agentId);

// After (replace all usages):
connectAgentChannels(agentId);
disconnectAgentChannels(agentId);

// Remove export aliases entirely
```

#### Python Imports

```python
# Before:
from new_location import NewClass as OldClass

# Usage:
instance = OldClass()

# After (update import and all usages):
from new_location import NewClass

instance = NewClass()
```

#### Python Constants

```python
# Before:
NEW_API_URL = "https://api.example.com"
OLD_API_URL = NEW_API_URL  # Backward compat

# Usage:
requests.get(OLD_API_URL)

# After (replace all usages):
requests.get(NEW_API_URL)

# Remove alias:
# (delete OLD_API_URL = NEW_API_URL line)
```

#### Python Properties

```python
# Before:
class Config:
    @property
    def new_value(self) -> str:
        return self._new_value

    @property
    def old_value(self) -> str:
        """Deprecated: use new_value"""
        return self.new_value

# Usage:
config.old_value

# After (replace all usages):
config.new_value

# Remove property:
# (delete old_value property)
```

### Step 4: Remove Alias Definition

After all usages are migrated:

```python
# Before:
NEW_CONFIG = {...}
OLD_CONFIG = NEW_CONFIG  # Backward compat - TODO: remove

# After:
NEW_CONFIG = {...}
# (delete OLD_CONFIG line)
```

### Step 5: Validation

```bash
# Verify old name is no longer used (should have 0 matches)
rg --type py --type ts '\bOLD_NAME\b'

# Run type checker (ensure no broken imports/references)
mypy path/to/modified/files.py
tsc --noEmit  # TypeScript

# Run tests (ensure behavior unchanged)
pytest path/to/tests/
npm test  # TypeScript/JavaScript

# Check for broken documentation references
rg --type md '\bOLD_NAME\b'
```

## When to Keep Aliases (Don't Remove)

These patterns have **legitimate reasons** for aliasing:

### 1. Public API Stability (External Callers)

```python
# KEEP: Public API used by external packages
# This is a public library, renaming would break downstream users
class OldAPIClient:  # Alias for NewAPIClient
    """Deprecated: Use NewAPIClient. Kept for backward compatibility."""
    pass

# If external packages depend on this, keep alias until major version bump
# Document deprecation and provide migration guide
```

**Verification**: Check if codebase is a library with external users.

### 2. Database Schema / External Data Contracts

```python
# KEEP: Aliasing for database column that can't be renamed
class User(Base):
    email_address: Mapped[str] = mapped_column("email")  # DB column is "email"

    @property
    def email(self) -> str:
        """Alias for backward compatibility with existing code."""
        return self.email_address

# Database schema changes are expensive; alias is justified
```

**Verification**: Check if renaming requires database migration affecting production data.

### 3. Gradual Migration (Document with TODO and Timeline)

```python
# KEEP TEMPORARILY: Gradual migration in progress
# TODO(2025-12-01): Remove after all services migrated to new_endpoint
OLD_ENDPOINT = NEW_ENDPOINT  # Backward compat during rollout

# Acceptable if:
# - TODO has specific deadline/milestone
# - Migration plan exists
# - Will be cleaned up (not indefinite)
```

**Verification**: Check if TODO has concrete timeline and is being tracked.

### 4. Semantic Clarity (Different Meaning)

```python
# KEEP: Not actually an alias - different semantic meaning
MAX_RETRIES = 3
NETWORK_RETRIES = MAX_RETRIES  # Semantic clarity: network operations use max retries

# This provides semantic clarity - NETWORK_RETRIES explains WHY this value
# Different from pure backward compat alias
```

**Verification**: Does the alias name provide additional semantic information?

## Decision Framework: Remove or Keep?

For each alias candidate, ask:

### 1. **External API Test**

- [ ] Is this used by external packages/callers? → **KEEP** (or deprecate gradually with major version bump)
- [ ] Is this internal-only code? → Proceed to next test

### 2. **Data Contract Test**

- [ ] Does this alias external data (database columns, API fields)? → **KEEP** (or plan expensive migration)
- [ ] Is this pure code-level alias? → Proceed to next test

### 3. **Migration Status Test**

- [ ] Is there a TODO with timeline for removal? → **KEEP TEMPORARILY** (track and remove on schedule)
- [ ] Is there gradual rollout in progress? → **KEEP TEMPORARILY** (document plan)
- [ ] No migration plan or indefinite backward compat? → **REMOVE** (create migration now)

### 4. **Semantic Clarity Test**

- [ ] Does alias name provide additional semantic meaning? → **KEEP** (not a pure alias)
- [ ] Is it pure duplication for backward compat? → **REMOVE**

### 5. **Usage Count Test**

- [ ] Is old name unused (0 references)? → **REMOVE IMMEDIATELY** (dead code)
- [ ] Is old name used 1-3 times? → **HIGH PRIORITY REMOVAL** (easy to migrate)
- [ ] Is old name used 10+ times? → **MEDIUM PRIORITY** (batched refactoring)

## Scan Results Structure

For each finding, apply the Decision Framework and categorize:

### ✅ Should Remove (High Priority)

```markdown
#### 1. TypeScript Function Name Aliases (stores_channels.ts)

**File:** `adgn/src/adgn/agent/web/src/features/chat/stores_channels.ts`
**Lines:** 45-46

**Evidence:**

- `export const connectAgentWs = connectAgentChannels`
- `export const disconnectAgentWs = disconnectAgentChannels`
- Comment: "Backward compatibility: function name aliases"

**Decision Framework Analysis:**

1. ✅ **External API**: Internal module, no external callers
2. ✅ **Usage count**: `connectAgentWs` used 0 times, `disconnectAgentWs` used 0 times
3. ✅ **Migration status**: No TODO, indefinite backward compat
4. ✅ **Semantic clarity**: Pure alias, no additional meaning

**Decision**: **REMOVE IMMEDIATELY** - Aliases are unused, no external dependencies

**Recommended fix:**

- Delete lines 45-46 (export aliases)
- Verify no usages: `rg '\b(connectAgentWs|disconnectAgentWs)\b'` → should be 0 matches
```

### ⚠️ Should Remove (Medium Priority - Has Usages)

```markdown
#### 2. Python Constant Alias (config.py)

**File:** `adgn/src/adgn/config.py`
**Line:** 23

**Evidence:**

- `OLD_API_URL = NEW_API_URL  # Backward compatibility`
- `OLD_API_URL` used 3 times in same file

**Decision Framework Analysis:**

1. ✅ **External API**: Internal config module
2. ⚠️ **Usage count**: 3 usages in same file
3. ✅ **Migration status**: No TODO, comment just says "backward compatibility"
4. ✅ **Semantic clarity**: Pure alias

**Decision**: **REMOVE** - Replace 3 usages with canonical name

**Recommended fix:**

1. Find all usages: `rg '\bOLD_API_URL\b'`
2. Replace with `NEW_API_URL` at all 3 call sites
3. Delete line 23 (`OLD_API_URL = NEW_API_URL`)
4. Verify: `rg '\bOLD_API_URL\b'` → should be 0 matches
```

### ✔️ Keep (Justified - Public API)

```markdown
#### 3. Public API Backward Compatibility (client.py)

**File:** `adgn/src/adgn/client.py`
**Line:** 156

**Evidence:**

- `OldClient = NewClient  # Backward compatibility for v1 API`
- Used by external packages (found in public docs)

**Why it should be kept:** **Public API stability**

- This is a public library with external users
- Breaking change would affect downstream packages
- Documented in public API docs

**Decision**: **KEEP** - Document deprecation, plan removal for next major version

**Recommended action:**

1. Add deprecation warning in docstring
2. Update docs to recommend `NewClient`
3. Plan removal for v3.0.0 (next major version)
4. Add to deprecation tracking issue
```

### ✔️ Keep (Justified - Gradual Migration)

```markdown
#### 4. Migration in Progress (endpoints.py)

**File:** `adgn/src/adgn/endpoints.py`
**Line:** 12

**Evidence:**

- `LEGACY_ENDPOINT = NEW_ENDPOINT  # TODO(2025-12): Remove after service migration`
- Used 15 times across codebase
- TODO has specific timeline

**Why it should be kept:** **Gradual migration with timeline**

- Explicit TODO with deadline (2025-12)
- Part of tracked migration effort
- Will be cleaned up on schedule

**Decision**: **KEEP TEMPORARILY** - Track in migration project

**Recommended action:**

1. Verify TODO is tracked in project management
2. Continue migration to NEW_ENDPOINT
3. Remove on schedule (2025-12)
4. Monitor usage count over time
```

## Validation

After removing aliases:

```bash
# 1. Verify old name has 0 references
rg --type py --type ts '\bOLD_NAME\b'
# Expected: No matches (or only in comments/docs)

# 2. Run type checker
mypy .
tsc --noEmit

# 3. Run tests
pytest
npm test

# 4. Check for broken imports
# Python: Look for import errors in test output
# TypeScript: Check for TS2305, TS2307 errors

# 5. Search documentation
rg --type md '\bOLD_NAME\b'
# Update any docs still referencing old name
```

## Benefits of Removing Legacy Aliases

✅ **Reduced cognitive load** - One canonical name to remember
✅ **Easier refactoring** - Only one name to update when changing APIs
✅ **Smaller codebase** - Less code to maintain
✅ **Better IDE support** - Autocomplete shows one clear option
✅ **Clearer intent** - No confusion about which name to use
✅ **Easier onboarding** - New developers see canonical names only

## References

- **Trivial Forwarders**: See `trivial-forwarders.md` for comprehensive function-level forwarder detection and decision framework
- **Trivial Forwarder Methods**: See `trivial-forwarder-methods.md` for method-level forwarders
- **API Stability**: [Semantic Versioning](https://semver.org/) for managing breaking changes
- **Deprecation Patterns**: [Python PEP 387](https://peps.python.org/pep-0387/) - Backwards Compatibility Policy
