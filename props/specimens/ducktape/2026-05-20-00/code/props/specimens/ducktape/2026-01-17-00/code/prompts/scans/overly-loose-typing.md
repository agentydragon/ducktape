# Scan: Overly Loose Input/Output Typing

## Context

@../shared-context.md

## Pattern Description

Functions with suspiciously loose type annotations that indicate "I don't know what this is" or "I gave up on types". These are footguns that allow garbage data to propagate through the system.

**Key principle**: Types should be as specific as possible. `Any`, `object`, `dict[str, Any]` are danger flags that indicate unclear data contracts.

## Examples of Antipatterns

### BAD: `Any` typed parameter - "I give up on types"

```python
# EXTREMELY BAD: Accepts literally anything, tries to stringify somehow
def _normalize_call_arguments(arguments: Any) -> str | None:
    if arguments is None or isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments)
    except TypeError:
        return str(arguments)

# Problem: Massive footgun - what if someone passes a file handle? A thread? A socket?
# This will silently call str() on garbage data
```

**Better**:

```python
def _normalize_call_arguments(arguments: dict[str, Any] | str | None) -> str | None:
    """Normalize function call arguments to JSON string.

    Args:
        arguments: Function arguments as dict (will be JSON-serialized),
                   pre-serialized JSON string, or None.
    """
    if arguments is None or isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)  # Now we know it's a dict
```

### BAD: `dict[str, Any]` return when actual type is known

```python
# BAD: Returns dict when we know it's from a Pydantic model
async def get_cached_response_payload(self, key: str) -> dict[str, Any] | None:
    snapshot_model = await self._get_snapshot(key)
    if snapshot_model.status != ResponseStatus.COMPLETE:
        return None
    # snapshot_model.response is OpenAIResponse, but we throw away the type!
    result: dict[str, Any] = snapshot_model.response.model_dump(mode="json")
    return result
```

**Better**:

```python
async def get_cached_response_payload(self, key: str) -> OpenAIResponse | None:
    snapshot_model = await self._get_snapshot(key)
    if snapshot_model.status != ResponseStatus.COMPLETE:
        return None
    return snapshot_model.response  # Return the typed object
```

### BAD: Overly permissive union - false "convenience"

```python
# BAD: Accepts both dict and str "for convenience"
def make_item_tool_call(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any] | str  # Ambiguous!
) -> FunctionCallItem:
    args_json = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    return FunctionCallItem(call_id=call_id, name=name, arguments=args_json)

# Problem: This is NOT convenient, it's ambiguous!
# - Callers must remember "do I have dict or str?"
# - Function doesn't express clear intent
# - Runtime isinstance() check is a code smell
```

**Why this "convenience" is actually worse**:

1. **If caller has pre-serialized JSON** → They should deserialize it first

   ```python
   # Don't do this:
   make_item_tool_call(name="foo", arguments=json_string)  # Pass pre-serialized

   # Do this instead:
   args_dict = json.loads(json_string)
   make_item_tool_call(name="foo", arguments=args_dict)  # Explicit
   ```

2. **If caller has dict** → They pass it directly

   ```python
   make_item_tool_call(name="foo", arguments={"key": "value"})
   ```

3. **API should be clear about what it wants**:

   ```python
   # GOOD: Clear, unambiguous API
   def make_item_tool_call(
       *,
       call_id: str,
       name: str,
       arguments: dict[str, Any]  # Clearly wants structured data
   ) -> FunctionCallItem:
       args_json = json.dumps(arguments)
       return FunctionCallItem(call_id=call_id, name=name, arguments=args_json)

   # If you really have JSON string, deserialize it at call site:
   make_item_tool_call(
       call_id="abc",
       name="foo",
       arguments=json.loads(json_string)  # Caller's responsibility
   )
   ```

**Why loose unions are bad**:

- **Ambiguity**: Function signature doesn't communicate intent
- **Runtime checking**: `isinstance()` checks are type system failure
- **False economy**: "Saving" one `json.loads()` call creates unclear API
- **Propagates looseness**: Callers don't know which form to use
- **Testing burden**: Must test both code paths

**Principle**: Force callers to be explicit. One `json.loads()` at call site is better than ambiguous API.

### BAD: Union with loose type - "sometimes I know, sometimes I don't"

```python
# BAD: Either return the typed model OR return a loose dict?
def get_user_data(user_id: str) -> User | dict[str, Any]:
    if cached := cache.get(user_id):
        return cached  # dict[str, Any]
    user = db.fetch_user(user_id)
    return user  # User

# Problem: Caller has to handle two completely different return types
# What fields exist in the dict? We don't know!
```

**Better**:

```python
def get_user_data(user_id: str) -> User:
    if cached := cache.get(user_id):
        return User.model_validate(cached)  # Parse dict to User
    return db.fetch_user(user_id)

# Or if cache stores serialized Users:
def get_user_data(user_id: str) -> User:
    cached = cache.get(user_id)
    return cached if cached else db.fetch_user(user_id)
    # Type: User | None from cache, User from db
```

## Detection Strategy

**MANDATORY Step 0**: Discover ALL unions, optionals, and loose type annotations.

- This scan is **required** - do not skip this step
- You **must** read and process ALL type annotation output using your intelligence
- High recall required, high precision NOT required - you determine which are overly loose
- Review each for: necessary looseness vs lazy typing, specific types available, runtime validation
- Prevents lazy analysis by forcing examination of ALL loose type patterns

```bash
# Find ALL Union type annotations
rg --type py '\| ' -B 1 -A 1 --line-number

# Find ALL Optional type annotations
rg --type py 'Optional\[' -B 1 -A 1 --line-number

# Find ALL Any type annotations
rg --type py ': Any' -B 1 -A 1 --line-number

# Find dict[str, Any] patterns
rg --type py 'dict\[str, Any\]' -B 1 -A 1 --line-number

# Find object typing
rg --type py ': object' -B 1 -A 1 --line-number

# Count totals
echo "Union types:" && rg --type py '\| ' | wc -l
echo "Optional types:" && rg --type py 'Optional\[' | wc -l
echo "Any types:" && rg --type py ': Any' | wc -l
```

**What to review for each loose type:**

1. **Can we know the actual type?** Pydantic model, TypedDict, specific union instead of Any?
2. **Is this from external source?** API response, user input requiring runtime validation?
3. **Is loose typing justified?** Check library docs, API specs, data source
4. **Runtime validation present?** isinstance() checks suggest we know the type
5. **Union too broad?** `str | int | float | bool | None` → probably should be specific

**Process ALL output**: Read each type annotation, use your judgment to identify overly loose patterns.

**Common overly loose patterns to flag:**

- `param: Any` - "I gave up on types"
- `-> dict[str, Any]` when returning from Pydantic model (use actual model type)
- `-> Any` returns - almost always wrong
- `param: dict[str, Any]` when structure is known (use TypedDict or Pydantic)
- `Union[str, int, float, ...]` kitchen sink unions (be more specific)

---

**Goal**: Find ALL instances of overly loose typing (100% recall target).

**Recall/Precision**: High recall (~95%) for syntactic patterns, low precision (~30-40%)

- `grep ": Any"` finds `Any` parameters: ~95% recall, ~40% precision (some legitimate uses)
- `grep ": dict\[str, Any\]"` finds loose dicts: ~95% recall, ~35% precision (many legitimate uses)
- `grep ": object"` finds object typing: ~90% recall, ~20% precision (rare but usually wrong)
- AST scan for unions with loose types: ~85% recall, ~30% precision

**Why low precision is expected**:

- `dict[str, Any]` is sometimes correct (truly dynamic data, JSON from external API)
- `Any` is sometimes needed (typing.Protocol variance, gradual typing migration)
- Need to understand: Is this loose typing necessary or lazy?

**Recommended approach AFTER Step 0**:

1. Run grep/AST to find ALL candidates (~95% recall, ~30-40% precision)
2. For each candidate, investigate:
   - **Can we know the actual type?** (Pydantic model, typed dict, specific union)
   - **Is this from an external source?** (API response, user input, config file)
   - **Is loose typing justified?** (Read library docs, check API specs)
   - **Does function immediately check types?** (Runtime validation suggests we know the type)
3. Fix confirmed loose typing with proper types
4. Document remaining loose types if truly necessary
5. **Supplement with manual reading** to find complex cases

**High-recall retrievers**:

### 1. Parameters Typed as `Any`

```bash
# Find function parameters typed as Any
rg --type py "def \w+\([^)]*: Any"

# Find parameters in method signatures
rg --type py "^\s+def \w+\([^)]*: Any"
```

**High priority**: `Any` parameters are almost always wrong - usually means "I gave up"

### 2. Returns Typed as `dict[str, Any]`

```bash
# Find functions returning dict[str, Any]
rg --type py "-> dict\[str, Any\]"

# Find functions returning dict[str, Any] | None
rg --type py "-> dict\[str, Any\] \| None"
```

**Investigation needed**: Does function return data from a known Pydantic model?

### 3. Parameters Typed as `dict[str, Any]`

```bash
# Find parameters accepting dict[str, Any]
rg --type py ": dict\[str, Any\]"
```

**Medium priority**: Sometimes justified (external API data), but often should be Pydantic model

### 4. Overly Permissive Unions

```bash
# Find unions mixing specific types with loose types
rg --type py ": \w+ \| dict\[str, Any\]"
rg --type py ": dict\[str, Any\] \| \w+"

# Find unions mixing dict and str
rg --type py ": dict\[str, Any\] \| str"
```

**Investigation needed**: Why does function accept multiple unrelated types?

### 5. AST-Based Discovery (Comprehensive)

Build tool that analyzes:

```python
# Pseudocode for AST-based detection
for func in all_functions:
    for param in func.parameters:
        if param.annotation == "Any":
            yield HighPriorityCandidate(func, param, reason="Any parameter")

        if param.annotation == "dict[str, Any]":
            # Check if function immediately validates/parses this
            if has_pydantic_validation_in_body(func, param.name):
                yield MediumPriorityCandidate(
                    func, param,
                    reason="dict[str, Any] with validation - should use Pydantic model"
                )
            else:
                yield Candidate(func, param, reason="dict[str, Any] parameter")

    # Check return types
    if func.return_type == "dict[str, Any]":
        if returns_model_dump(func):
            yield HighPriorityCandidate(
                func, None,
                reason="Returns model_dump() but typed as dict[str, Any]"
            )
```

**Verification for each candidate**:

1. **For `Any` parameters**:
   - Read function body: Does it immediately check `isinstance()`?
   - If yes → We know the actual type, should use union
   - If no → Investigate what this parameter actually is

2. **For `dict[str, Any]` returns**:
   - Trace back to source: Is this from `model.model_dump()`?
   - If yes → Should return the Pydantic model type
   - If from external API → Check if we have a Pydantic model for it
   - If truly dynamic → Keep `dict[str, Any]` but document why

3. **For unions with loose types**:
   - Why does function accept multiple types?
   - Read library documentation / API specs
   - Is this handling multiple data sources? (Should be separate functions)
   - Is this matching an external API requirement? (Document it)

4. **For `dict[str, Any]` parameters**:
   - Is this immediately validated with Pydantic?
   - If yes → Parameter should be the Pydantic model type
   - Is this from external API?
   - If yes → Create Pydantic model for the API schema

## Investigation Process

For each loose type found:

### Step 1: Understand the Source

```python
# Trace back: Where does this data come from?
# - Pydantic model.model_dump()? → Use the Pydantic type
# - External API response? → Create Pydantic model for API
# - User input? → Validate and parse to structured type
# - Config file? → Define typed config schema
```

### Step 2: Check for Runtime Validation

```python
# If function does this:
def process_data(data: Any):
    if isinstance(data, dict):
        # ... work with dict
    elif isinstance(data, str):
        # ... work with str
    else:
        raise ValueError("Invalid data type")

# Then it should be:
def process_data(data: dict[str, Any] | str):
    if isinstance(data, dict):
        # ... work with dict
    else:
        # ... work with str (no else case needed)
```

### Step 3: Read External Documentation

```python
# For library integrations, check:
# 1. Does OpenAI API accept both dict and str?
# 2. Read openai-python source code
# 3. Read FastMCP documentation
# 4. Verify the loose typing is truly required

# If required by external API:
def api_function(param: dict[str, Any] | str):
    """
    Args:
        param: Accepts both dict and str to match OpenAI API specification.
               See: https://platform.openai.com/docs/api-reference/...
    """
    pass
```

### Step 4: Create Proper Types

```python
# Instead of:
def get_config(name: str) -> dict[str, Any]:
    return json.loads(config_file.read_text())

# Define the structure:
class AppConfig(BaseModel):
    database_url: str
    api_key: str
    timeout: int = 30

def get_config(name: str) -> AppConfig:
    data = json.loads(config_file.read_text())
    return AppConfig.model_validate(data)
```

## Fix Strategy

### Fix 1: Replace `Any` with Specific Union

```python
# Before:
def process(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data)

# After:
def process(data: dict[str, Any] | str) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data)
```

### Fix 2: Replace `dict[str, Any]` Return with Pydantic Model

```python
# Before:
def get_user(user_id: str) -> dict[str, Any]:
    user = db.fetch_user(user_id)
    return user.model_dump()

# After:
def get_user(user_id: str) -> User:
    return db.fetch_user(user_id)
```

### Fix 3: Create Pydantic Model for External API

```python
# Before:
def fetch_github_user(username: str) -> dict[str, Any]:
    response = requests.get(f"https://api.github.com/users/{username}")
    return response.json()

# After:
class GitHubUser(BaseModel):
    login: str
    id: int
    avatar_url: str
    html_url: str
    # ... other fields

def fetch_github_user(username: str) -> GitHubUser:
    response = requests.get(f"https://api.github.com/users/{username}")
    return GitHubUser.model_validate(response.json())
```

### Fix 4: Document When Loose Typing is Necessary

```python
# If truly needed (rare):
def process_json_payload(payload: dict[str, Any]) -> None:
    """Process arbitrary JSON payload from webhook.

    Args:
        payload: Arbitrary JSON from external webhook. Structure varies by
                 webhook source and cannot be known at development time.
                 Validation happens via JSONSchema at runtime.
    """
    schema = get_schema_for_source(payload.get("source"))
    validate(payload, schema)
    # ... process
```

## When Loose Typing is Actually Justified

Rare cases where loose typing is correct:

### 1. Truly Dynamic External Data

```python
# Webhook payloads that vary by source
def handle_webhook(payload: dict[str, Any]) -> None:
    # Can't know structure at development time
    # Validated against JSONSchema at runtime
```

### 2. Generic JSON Processing

```python
# Library function that works with any JSON
def pretty_print_json(data: dict[str, Any] | list[Any]) -> str:
    return json.dumps(data, indent=2)
```

### 3. NOT JUSTIFIED: "Convenience" Unions

**Even if external API accepts multiple forms, your wrapper should pick one**:

```python
# ❌ BAD: Mirroring external API's loose typing
def api_call(data: dict[str, Any] | str) -> Response:
    """Calls external API that accepts dict or JSON string."""
    # ... handle both cases

# ✅ GOOD: Pick the structured form, force callers to be explicit
def api_call(data: dict[str, Any]) -> Response:
    """Calls external API.

    Args:
        data: Request payload. Will be JSON-serialized internally.
              If you have pre-serialized JSON, deserialize it first.
    """
    json_str = json.dumps(data)
    return requests.post(url, data=json_str)

# Callers with JSON string must be explicit:
api_call(json.loads(json_string))  # Clear: deserialize then call
```

**Reason**: Your API should have ONE clear contract, even if underlying library is permissive. Force callers to be explicit about what they're passing.

## Validation

```bash
# After fixing loose types:
mypy --strict path/to/file.py

# Should reveal if we broke anything:
# - If mypy happy: loose types were unnecessary
# - If mypy complains: might need to add type narrowing
```

## Benefits

✅ **Type safety** - Catch bugs at development time, not runtime
✅ **Better IDE support** - Autocomplete knows what fields exist
✅ **Self-documenting** - Types show what data is expected
✅ **Easier refactoring** - Type checker catches all usages
✅ **Prevents footguns** - Can't pass garbage data anymore
