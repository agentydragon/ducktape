# Scan: Pydantic Antipatterns

## Context

@../shared-context.md

## Pattern 1: Manual Field-by-Field model_dump

### Seen in: adgn/rspcache/models.py

```python
# BAD: Repetitive manual dumping per field
def to_db_payload(self) -> dict[str, Any]:
    return {
        "status": self.status.value,
        "response_json": self.response.model_dump(mode="json") if self.response else None,
        "error_json": self.error.model_dump(mode="json") if self.error else None,
        "token_usage_json": self.token_usage.model_dump(mode="json") if self.token_usage else None,
    }

# GOOD: Let Pydantic handle it
def to_db_payload(self) -> dict[str, Any]:
    return self.model_dump(mode="json")
```

Issues:

- Repetitive `model_dump(mode="json") if ... else None`
- Fragile: adding fields needs manual updates
- Not using Pydantic's nested serialization

## Pattern 2: Manual Field-by-Field model_validate

### Seen in: adgn/rspcache/models.py

```python
# BAD: Manual validation replicating what Pydantic does
@classmethod
def from_db(cls, *, status: str, response: Any, error: Any, token_usage: Any) -> FinalResponseSnapshot:
    return cls(
        status=ResponseStatus(status),
        response=parse_response(response) if response is not None else None,
        error=parse_error(error) if error is not None else None,
        token_usage=parse_usage(token_usage) if token_usage is not None else None,
    )

# GOOD: Use model_validate directly
def to_model(self) -> FinalResponseSnapshot:
    return FinalResponseSnapshot.model_validate({
        "status": self.status,
        "response": self.response,
        "error": self.error,
        "token_usage": self.token_usage,
    })
```

Issues:

- Manual field-by-field validation/parsing
- Doesn't leverage Pydantic's built-in validation
- Extra boilerplate code

## Pattern 3: Dict-Style Access on Pydantic Model Fields

### Seen in: adgn/inop/grading/strategies.py, adgn/inop/engine/models.py

```python
# BAD: Using dict-style access on Pydantic model fields
class ComparisonGrading(BaseModel):
    criteria: list[dict[str, str]]  # Should be list[Criterion]!

criteria_desc = "\n".join([f"- {c['name']}: {c['description']}" for c in criteria])

# GOOD: Use Pydantic models with attribute access
class ComparisonGrading(BaseModel):
    criteria: list[Criterion]

criteria_desc = "\n".join([f"- {c.name}: {c.description}" for c in criteria])
```

Issues:

- Using `list[dict[str, str]]` when a Pydantic model already exists (e.g., `Criterion`)
- Dict-style access (`obj['field']`) instead of attribute access (`obj.field`)
- Loses type safety and IDE autocomplete
- Doesn't leverage Pydantic validation
- Makes code harder to refactor

**Detection aid**:

```bash
# Find potential dict-access on model fields (check each manually)
rg --type py "\[\"(name|description|criteria|score|rationale|status|type)\"\]"
```

**When dict access is OK**:

- Accessing raw JSON data from `json.loads()` before creating Pydantic models
- Generic dict operations where the structure isn't a domain model
- Dynamic field access in meta-programming contexts

**When to fix**:

- A Pydantic model already exists for the structure
- The fields are well-known and fixed
- The code would benefit from type safety

## Pattern 4: Dicts/Serialized Forms Outside Serialization Boundaries

**CRITICAL PRINCIPLE**: Dicts and serialized forms (JSON, dict[str, Any]) should **ONLY** appear at **serialization/deserialization boundaries** (request/response handling, file I/O, database I/O).

**Anywhere else in the codebase, dicts are SUSPECT and should be INVESTIGATED THOROUGHLY.**

### BAD: Dicts flowing through internal code

```python
# BAD: Accepting dict in internal function
def compute_cache_key(body: dict[str, Any]) -> str:
    """Internal logic operating on untyped dict."""
    # Manual filtering - no type safety
    keyed = {k: v for k, v in body.items() if k not in ("request_id", "timestamp")}
    return hash(canonicaljson.encode_canonical_json(keyed))

@app.post("/chat")
async def chat_endpoint(request: Request):
    # Boundary: deserialize
    body = await request.json()  # dict[str, Any]

    # BAD: Passing dict into internal logic
    key = compute_cache_key(body)  # ← dict escapes boundary!

    # More internal logic with dict
    response = await process_request(body)  # ← dict everywhere!
    return response

# BAD: Returning dict from internal function
def build_response(data: SomeModel) -> dict[str, Any]:
    """Internal logic returning dict."""
    return {
        "status": data.status,
        "result": data.result.model_dump() if data.result else None,
    }

# Even worse: dict[str, Any] in domain models
class CacheEntry(BaseModel):
    key: str
    data: dict[str, Any]  # ❌ Untyped blob in domain model!
```

### GOOD: Pydantic models internally, dicts only at boundaries

```python
# GOOD: Internal functions use Pydantic models
def compute_cache_key(body: ResponseCreateParams) -> CacheKey:
    """Type-safe internal logic."""
    # Use model_copy for field exclusion - validated
    cacheable = body.model_copy(update={"request_id": None, "timestamp": None})
    keyed = cacheable.model_dump(mode="json", exclude_none=True)
    return CacheKey(hashlib.sha256(canonicaljson.encode_canonical_json(keyed)).hexdigest())

@app.post("/chat")
async def chat_endpoint(request: Request):
    # Boundary: deserialize dict → Pydantic
    body_dict = await request.json()
    body = ResponseCreateParams.model_validate(body_dict)  # ✓ Convert immediately

    # GOOD: All internal logic uses typed model
    key = compute_cache_key(body)
    response = await process_request(body)

    # Boundary: serialize Pydantic → dict (FastAPI does this automatically)
    return response  # FastAPI serializes Pydantic to JSON

# GOOD: Return Pydantic, let framework serialize
def build_response(data: SomeModel) -> ResponseModel:
    """Return structured type, framework handles serialization."""
    return ResponseModel(
        status=data.status,
        result=data.result,  # Pydantic handles nested serialization
    )

# GOOD: Fully typed domain models
class CacheEntry(BaseModel):
    key: str
    data: ResponseData  # ✓ Structured type, not dict blob
```

### Correct Architecture: Thin Boundaries, Structured Interior

```
┌─────────────────────────────────────────────────┐
│  HTTP Request (bytes/JSON)                       │  ← Serialization boundary
└──────────────────┬──────────────────────────────┘
                   │ model_validate()
                   ▼
┌─────────────────────────────────────────────────┐
│  Pydantic Models (ResponseCreateParams, etc.)   │  ← INTERNAL: Fully typed
│                                                  │
│  • compute_cache_key(body: ResponseCreateParams) │
│  • process_request(body: ResponseCreateParams)   │
│  • All internal logic: Pydantic models          │
└──────────────────┬──────────────────────────────┘
                   │ model_dump() or FastAPI auto-serialize
                   ▼
┌─────────────────────────────────────────────────┐
│  HTTP Response (bytes/JSON)                      │  ← Serialization boundary
└─────────────────────────────────────────────────┘
```

**Key principle**: "Parse, don't validate" - convert untyped data to typed models as soon as possible, keep typed representation throughout internal logic.

### Why This Matters

**Dicts are problematic because:**

- **No type safety**: Typos in keys caught at runtime, not compile time
- **No IDE support**: No autocomplete, no refactoring
- **Unclear schema**: What keys exist? What are their types?
- **Easy to corrupt**: Can add arbitrary keys, wrong types
- **Poor documentation**: Schema hidden in code, not declarative

**Pydantic models solve this:**

- **Type checked**: mypy catches errors
- **IDE support**: Autocomplete, go-to-definition, refactoring
- **Clear schema**: Model definition is documentation
- **Validation**: Pydantic validates on construction
- **Serialization**: Handles nested models, enums, dates automatically

### Detection

**Grep patterns** (high recall, review each):

```bash
# Find dict[str, Any] type annotations (review for boundary vs internal)
rg --type py 'dict\[str, Any\]'

# Find Any type annotations (often indicates untyped data)
rg --type py ': Any[,\]]'

# Find object type (overly loose)
rg --type py ': object[,\)]'

# Find model_dump() in internal code (not at boundaries)
rg --type py 'model_dump\(' --glob '!**/routes.py' --glob '!**/endpoints.py'

# Find .json() calls not immediately followed by model_validate
rg --type py -A2 '\.json\(\)' | grep -v 'model_validate'

# Find functions accepting dict parameters
rg --type py 'def \w+\(.*: dict\['
```

**Manual review checklist:**

1. Is this at a serialization boundary? (HTTP endpoint, file I/O, DB I/O)
   - YES → dict acceptable (but immediately convert to Pydantic)
   - NO → should use Pydantic model
2. Does a Pydantic model already exist for this structure?
   - YES → use it
   - NO → create one if structure is well-defined
3. Is the structure truly dynamic/schemaless?
   - Rare - most "dynamic" data has implicit schema
   - If truly schemaless, consider tagged union instead

### Fix Strategy

1. **Identify boundaries**: Find where data enters/exits system
   - HTTP requests/responses
   - File reads/writes
   - Database queries/results
   - External API calls

2. **Convert at boundaries**:

   ```python
   # At input boundary
   raw_dict = await request.json()
   model = MyModel.model_validate(raw_dict)  # ← Convert immediately

   # At output boundary
   return model  # ← FastAPI serializes automatically
   # OR if manual: return model.model_dump(mode="json")
   ```

3. **Update internal functions**:
   - Change `def func(data: dict[str, Any])` → `def func(data: MyModel)`
   - Change `data["field"]` → `data.field`
   - Remove manual validation/parsing code

4. **Create models for missing types**:
   - If dict structure is well-defined but no model exists, create one
   - Use Pydantic's nested model support for complex structures

## Pattern 3: Union Types Mixing Pydantic Models with Weak Types

**Core principle**: If you're doing `isinstance(x, BaseModel)` checks, you're not using Pydantic's type system properly.

### Antipattern: Runtime Type Checking on Pydantic Unions

```python
# BAD: Union allows weak types, requires runtime checking
def norm_item(x: BaseModel | str | dict[str, Any]) -> dict[str, Any] | str:
    """Normalize an item for OpenAI API compatibility."""
    if isinstance(x, BaseModel):
        return x.model_dump(exclude_none=True)
    return x

# Used with:
class ResponsesRequest(BaseModel):
    input: list[InputItem] | str  # InputItem = union of Pydantic models

# Problem: Callers pass dicts instead of Pydantic models
payload["input"] = [norm_item(it) for it in input_value]
```

**Why this is bad:**

- **Type system defeat**: Union with `dict` or `Any` defeats Pydantic's validation
- **Runtime checking smell**: `isinstance(x, BaseModel)` means callers aren't using proper types
- **Lost validation**: Passing raw dicts bypasses Pydantic's validation
- **API ambiguity**: Callers don't know whether to pass models or dicts

### Root Cause: Callers Not Using Pydantic Models

The union exists because callers do this:

```python
# BAD: Passing weak types
request = ResponsesRequest(input=[
    {"role": "user", "content": "..."},  # Dict instead of UserMessage
    "some string",                        # Mixed types
])
```

Instead of:

```python
# GOOD: Passing proper Pydantic models
request = ResponsesRequest(input=[
    UserMessage.text("..."),  # Proper Pydantic model
])
```

### Fix Strategy

1. **Find all construction sites** where the union-typed field is populated
2. **Make callers construct proper Pydantic models**:

   ```python
   # Before: Passing dict
   ResponsesRequest(input=[{"role": "user", "content": "hello"}])

   # After: Passing Pydantic model
   ResponsesRequest(input=[UserMessage.text("hello")])
   ```

3. **Remove weak types from union** - change `list[InputItem] | str | dict` to `list[InputItem]`
4. **Remove runtime type checking** - `norm_item` becomes trivial or inlineable:

   ```python
   # Before: Needed runtime checking
   payload["input"] = [norm_item(it) for it in input_value]

   # After: All items are BaseModel, no checking needed
   payload["input"] = [x.model_dump(exclude_none=True) for x in input_value]
   ```

5. **Optional: Add factory methods** for convenience if deserved:

   ```python
   @classmethod
   def from_text(cls, text: str) -> "UserMessage":
       return cls(content=[InputTextPart(text=text)])
   ```

### When Union Types ARE Acceptable

**Legitimate uses:**

- **I/O boundaries**: Deserializing from external API that sends different formats

  ```python
  # OK: External API sends either format
  class Response(BaseModel):
      data: SuccessData | ErrorData  # Both are Pydantic models
  ```

- **Multiple Pydantic models**: Union of concrete Pydantic types

  ```python
  # OK: All union members are Pydantic models
  InputItem = UserMessage | AssistantMessage | SystemMessage
  ```

**Code smell:**

- **Mixing Pydantic with weak types**: `BaseModel | dict | Any | str`
- **Runtime isinstance checks**: If you check `isinstance(x, BaseModel)`, fix callers

### Detection

**MANDATORY grep patterns** (high recall - surfaces all union type smells):

```bash
# Find union types mixing BaseModel with weak types
rg --type py 'BaseModel.*\|.*(dict|Any|str)'
rg --type py '(dict|Any|str).*\|.*BaseModel'

# Find isinstance checks on BaseModel (code smell - should be rare)
rg --type py 'isinstance\(.*BaseModel\)'

# Find union types in Pydantic field definitions
rg --type py -A2 'class.*\(BaseModel\)' | rg ': .*\|'
```

**Review each match:**

- Is this at an I/O boundary (deserializing external data)? → Might be OK
- Is this internal code where callers should pass proper types? → FIX
- Does the code have `isinstance(x, BaseModel)` checks? → Strong smell

## Detection Strategy

**MANDATORY first step**: Run grep patterns to find ALL Pydantic union type smells and runtime checks.

- This scan is **required** - do not skip this step
- You **must** read and handle the complete grep output
- Do not sample or skip any results - process every union type and isinstance check found
- Prevents lazy analysis by forcing examination of all Pydantic type usage patterns

**High-priority patterns** (run these first - strong smells):

```bash
# 1. Union types mixing BaseModel with weak types (STRONGEST SMELL)
rg --type py 'BaseModel.*\|.*(dict|Any|str)' --line-number
rg --type py '(dict|Any|str).*\|.*BaseModel' --line-number

# 2. isinstance checks on BaseModel (should be VERY rare in healthy code)
rg --type py 'isinstance\(.*BaseModel\)' --line-number

# 3. Union types in Pydantic field definitions (review all)
rg --type py -A2 'class.*\(BaseModel\)' | rg ': .*\|' --line-number
```

**Secondary patterns** (lower priority - may have valid reasons):

```bash
# Find manual field-by-field model_dump patterns
rg --type py -A5 "def (to_db|to_dict|serialize)" | rg "model_dump.*if.*else"

# Find manual field-by-field validation classmethods
rg --type py -A10 "@classmethod" | rg -B3 "return cls\("

# Find complex model_validator with multiple type branches
rg --type py -A10 '@model_validator.*mode="before"'
```

**Manual review for each match:**

1. **Union with weak types** (`BaseModel | dict`):
   - Is this at I/O boundary deserializing external data? → Might be OK
   - Is this internal code where callers control the type? → **FIX CALLERS**

2. **isinstance(x, BaseModel)** checks:
   - Why is this needed? Usually means callers pass wrong types
   - Fix callers to pass proper Pydantic models
   - After fix, this check becomes unnecessary

3. **Manual serialization/validation**:
   - Does Pydantic already handle this? → Remove manual code
   - Is custom logic truly necessary? → Document why

## Fix Strategy

1. **For serialization (model_dump)**:
   - If field names match: Just use `model_dump(mode="json")`
   - If enum needs `.value`: Use `@field_serializer`
   - If fields need different names: Rename DB columns or use separate DB model class

2. **For deserialization (model_validate)**:
   - Replace manual `from_db()`-style methods with `model_validate(dict)`
   - Let Pydantic handle type conversion and validation
   - Only keep custom logic if truly needed (custom parsing, migration, etc.)

## References

- [Pydantic Serialization](https://docs.pydantic.dev/latest/concepts/serialization/)
- [Pydantic Validation](https://docs.pydantic.dev/latest/concepts/validators/)
