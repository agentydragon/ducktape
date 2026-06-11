# Scan: Manual Serialization Patterns That Should Use Pydantic

## Context

@../shared-context.md

## Pattern Description

Code using manual JSON serialization/deserialization, dict construction, and validation instead of leveraging Pydantic's built-in capabilities.

## Core Principle

**String-literal keyed dict access in internal code is suspect.** If you're accessing `data["key"]` with a literal string, and the keys are known at development time, you should use a Pydantic model.

## Examples of Antipatterns

### 1. list[dict] parameters with string-literal access

```python
# BAD: Dict with known structure passed around internal code
def __init__(self, container_files: list[dict[str, str]]):
    """container_files: List of dicts with 'path' and 'content' keys."""
    self._files = {f["path"]: f["content"] for f in container_files}
    # Problems:
    # - Magic strings "path" and "content"
    # - No validation - can pass {"foo": "bar"}
    # - KeyError at runtime instead of type error
    # - Structure documented in docstring instead of enforced by types

# GOOD: Pydantic model with type safety
class ContainerFile(BaseModel):
    path: str
    content: str

def __init__(self, container_files: list[ContainerFile]):
    self._files = {f.path: f.content for f in container_files}
    # Benefits:
    # - Type-safe property access
    # - Automatic validation
    # - IDE autocomplete
    # - Can't pass wrong structure
```

**When is `list[dict]` OK?**

- **I/O boundaries**: Reading JSON from external API, files
- **Dynamic schemas**: Keys/structure not known at development time
- **Passthrough**: Just forwarding data unchanged

**When is `list[dict]` BAD?**

- **Internal code**: Passing structured data between functions
- **Known structure**: Keys are documented/expected
- **Validation needed**: Structure should be enforced

### 2. Manual json.loads with dict validation

```python
# BAD: Manual JSON parsing and dict access
def load_config(data: str) -> dict:
    config = json.loads(data)
    if "name" not in config:
        raise ValueError("Missing name")
    if not isinstance(config["name"], str):
        raise TypeError("Invalid name type")
    return config

# GOOD: Pydantic handles validation
class Config(BaseModel):
    name: str

def load_config(data: str) -> Config:
    return Config.model_validate_json(data)
```

### 2. Dataclass + manual dict assembly

```python
# BAD: Dataclass with manual serialization
@dataclass
class UserData:
    id: str
    email: str
    created: datetime

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "created": self.created.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> UserData:
        return cls(
            id=data["id"],
            email=data["email"],
            created=datetime.fromisoformat(data["created"]),
        )

# GOOD: Pydantic handles it
class UserData(BaseModel):
    id: str
    email: str
    created: datetime

# Now just:
user.model_dump(mode="json")  # Auto-handles datetime
UserData.model_validate(data)  # Auto-parses datetime
```

### 3. Manual field extraction from JSON

```python
# BAD: Manual extraction pattern
data = json.loads(response_text)
mcp_config = MCPConfig.model_validate(json.loads(data["specs"])) if data["specs"] else MCPConfig()
metadata = json.loads(data["metadata"]) if data.get("metadata") else {}

# BETTER: Nested Pydantic models
class ResponseData(BaseModel):
    specs: MCPConfig | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

data = ResponseData.model_validate_json(response_text)
# Access: data.specs, data.metadata
```

### 4. Dict wrangling for nested structures

```python
# BAD: Manual dict construction
def build_request(user_id: str, items: list[str]) -> dict:
    return {
        "user": {
            "id": user_id,
            "preferences": {"lang": "en"}
        },
        "items": [{"name": item, "qty": 1} for item in items],
        "timestamp": datetime.now().isoformat()
    }

# GOOD: Nested Pydantic models
class UserPreferences(BaseModel):
    lang: str = "en"

class User(BaseModel):
    id: str
    preferences: UserPreferences = Field(default_factory=UserPreferences)

class Item(BaseModel):
    name: str
    qty: int = 1

class Request(BaseModel):
    user: User
    items: list[Item]
    timestamp: datetime = Field(default_factory=datetime.now)

def build_request(user_id: str, items: list[str]) -> Request:
    return Request(
        user=User(id=user_id),
        items=[Item(name=item) for item in items]
    )

# Serialize: request.model_dump(mode="json")
```

### 5. TypedDict for structure that should be BaseModel

```python
# BAD: TypedDict with manual validation
class EventPayload(TypedDict):
    event_type: str
    timestamp: str
    data: dict

def parse_event(raw: str) -> EventPayload:
    data = json.loads(raw)
    # Manual validation...
    if "event_type" not in data:
        raise ValueError("Missing event_type")
    return data  # No runtime validation!

# GOOD: Pydantic BaseModel
class EventPayload(BaseModel):
    event_type: str
    timestamp: datetime  # Auto-parsed!
    data: dict[str, Any]

def parse_event(raw: str) -> EventPayload:
    return EventPayload.model_validate_json(raw)
```

## When Pydantic Adds Value

Use Pydantic when you have:

- **Validation needs**: Type checking, constraints, custom validators
- **Nested structures**: Complex object hierarchies
- **Serialization**: Need JSON/dict conversion with proper type handling
- **Type coercion**: Auto-parsing (datetime, enums, etc.)
- **Schema generation**: OpenAPI, JSON Schema
- **Settings/config**: Environment variable loading, .env files

## Detection Strategy

**MANDATORY first step**: Run `scan_manual_serde.py` and process ALL output.

- This scan is **required** - do not skip this step
- You **must** read and handle the complete scan output (can pipe to temp file)
- Do not sample or skip any results - process every dict literal and Pydantic model found
- Prevents lazy analysis by forcing examination of all dict construction patterns

**Note**: This scan has high false positives (many dict literals are legitimate at I/O boundaries), but you must still process all results to ensure comprehensive review.

**Key questions for manual review**:

1. Is this I/O boundary or internal code passing data?
2. Are keys known at development time?
3. Is there documentation of dict structure in docstrings? (RED FLAG)
4. Are same literal keys accessed multiple times?

### Automated Scanning Tool

**Tool**: `prompts/scans/scan_manual_serde.py` - AST-based scanner for dict literals and Pydantic model analysis

**What it finds**:

1. **Dict literals with string keys** - All `{"key": value}` constructions in the codebase
   - Surfaces candidates for manual composition that should be Pydantic `__init__` calls
   - Particularly useful when dict is in internal code (not I/O boundary)
2. **Pydantic BaseModel classes** - All models with their fields and types
   - Raw data for you to analyze for design issues
   - You can identify: overlapping fields, duplicate field sets, single-field models, opportunities for shared sub-models

**Usage**:

```bash
# Run on entire codebase
python prompts/scans/scan_manual_serde.py . > serde_scan.json

# Run on specific directory
python prompts/scans/scan_manual_serde.py path/to/module > module_serde.json

# Pretty-print summary
python prompts/scans/scan_manual_serde.py . 2>&1 | grep "===" -A 10
```

**Output structure**:

- `summary`: Counts of dict literals and models found
- `dict_literals`: Dict mapping file paths to lists of `{line, col, keys, context}`
- `pydantic_models`: Dict mapping file paths to lists of `{line, name, fields}`

**Manual review workflow**:

1. **Review dict_literals**:
   - Check context: Is this in internal code or I/O boundary?
   - If internal + known keys → Candidate for Pydantic model
   - Look for patterns: same keys appearing in multiple places
   - Cross-reference with pydantic_models: Does a model already exist for this structure?

2. **Analyze pydantic_models for overlapping fields**:
   - Compare field sets across models
   - Models sharing 50%+ fields → Should they share a common base model?
   - Example: `UserRequest` and `UserResponse` both have `user_id, email, name`
   - Consider: Create `UserCore` base model, inherit in both

3. **Identify single-field models**:
   - Filter models where `len(fields) == 1`
   - Single-field models are often legitimate (NewType pattern, validation)
   - But review: Is this just a wrapper? Could it be a type alias?
   - Keep if: Custom validation, multiple methods, clear semantic meaning
   - Replace if: Just wrapping a primitive with no behavior

4. **Find duplicate field sets**:
   - Group models by identical field sets
   - Models with identical fields → Should one be eliminated?
   - Check: Are these in different modules for different contexts? (might be OK)
   - Consider: Consolidate into single model if semantically identical

**Example analysis**:

```bash
# After running scan, analyze output with jq
# Note: Output is grouped by file, so iterate over files then filter items

# Review dict literals with 3+ keys (more likely to need models)
cat serde_scan.json | jq '.dict_literals | to_entries[] | {file: .key, literals: [.value[] | select(.keys | length >= 3)]}'

# Focus on dict literals in functions (internal code, not module-level)
cat serde_scan.json | jq '.dict_literals | to_entries[] | {file: .key, literals: [.value[] | select(.context | contains("function"))]}'

# Find single-field models for review
cat serde_scan.json | jq '.pydantic_models | to_entries[] | {file: .key, models: [.value[] | select(.fields | length == 1)]}'

# You can analyze overlapping fields by comparing model field sets across all files
# Example: Load into Python and programmatically compare field sets
```

**Tool characteristics**:

- **Dict literal finding**: ~100% recall (finds all dict literals), ~20% precision (most are legitimate)
- **Model field extraction**: ~100% recall (finds all BaseModel classes and their fields)
- **False positives are expected**: This is a discovery tool, not a verdict
- **Manual verification required**: Context determines if each finding is actually problematic
- **You do the analysis**: Tool surfaces raw data; you analyze overlaps, duplicates, single-field models

**What the tool CANNOT tell you** (requires your judgment):

- Whether a dict literal is at an I/O boundary (legitimate) or internal code (suspicious)
- Whether overlapping fields indicate poor design or intentional separation of concerns
- Whether a single-field model is a useful abstraction or unnecessary wrapper
- Whether dict keys are truly "known at development time" or dynamic

### Grep Patterns

```bash
# String-literal dict access (suspicious in internal code, fine at I/O boundaries)
rg --type py '\["[a-zA-Z_]+"\]'  # f["path"], data["key"]
rg --type py "list\[dict\[str"    # list[dict[str, ...]] parameters

# Manual JSON with validation
rg --type py "json\.loads.*\n.*if.*not in"

# Dataclass with to_dict/from_dict
rg --type py -A5 "@dataclass" | grep -A3 "def (to_dict|from_dict|asdict)"

# Manual datetime.isoformat() in dict construction
rg --type py "\.isoformat\(\)" | rg "\{|\["

# TypedDict (often should be BaseModel)
rg --type py "class \w+\(TypedDict\)"

# Manual field extraction patterns
rg --type py "json\.loads.*\[.*\].*if.*else"

# Dict construction with known keys in internal functions
rg --type py "return \{.*:" --multiline  # Look for manual dict returns
```

### Context Analysis (Semantic, Not Just Pattern)

**Key questions to ask:**

1. **Is this I/O or internal code?**
   - I/O boundary (reading JSON, calling external API): `dict` is OK
   - Internal function passing data: `dict` is suspicious

2. **Are keys known at development time?**
   - Yes (e.g., always "path" and "content"): Should be Pydantic
   - No (keys from user input, config): `dict` is fine

3. **Is there documentation of dict structure?**
   - Docstring says "dict with keys X, Y, Z": RED FLAG - should be model
   - No structure mentioned: Might be genuinely dynamic

4. **Are there multiple accesses with same literal keys?**
   - `data["key"]` appears 3+ times: Should be a model
   - One-off access: Might be fine

### AST-Based Discovery (Optional)

Build tool that flags dataclasses with manual serde methods:

- Visit ClassDef nodes with `@dataclass` decorator
- Check for `to_dict()`, `from_dict()`, or `asdict()` methods
- Flag for manual review

Strong coding LLM can build this from description. Use output as discovery only - some manual serde is legitimate.

## Examples from This Codebase

### agent_server/persist/sqlite.py

```python
# Current pattern:
mcp_config=MCPConfig.model_validate(json.loads(r["specs"]))
    if r["specs"]
    else MCPConfig(),
metadata=meta_val,

# Could be improved with nested Pydantic:
class AgentRowDB(BaseModel):
    id: str
    created_at: datetime
    specs: MCPConfig = Field(default_factory=MCPConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)

# Then just:
row = AgentRowDB.model_validate(db_row_dict)
```

## Fix Strategy

### Step 1: Identify the data structure

- What fields exist?
- What types are they?
- Any validation rules?
- Any nested structures?

### Step 2: Create Pydantic model(s)

```python
class MyModel(BaseModel):
    field1: str
    field2: int
    nested: NestedModel | None = None

    model_config = ConfigDict(
        # Add config as needed
        str_strip_whitespace=True,
        validate_default=True,
    )
```

### Step 3: Replace manual code

```python
# Replace:
data = json.loads(text)
obj = SomeClass(data["field1"], data["field2"])

# With:
obj = MyModel.model_validate_json(text)
```

### Step 4: Update serialization

```python
# Replace:
output = json.dumps({"field1": obj.field1, "field2": obj.field2})

# With:
output = obj.model_dump_json()
```

## When NOT to Use Pydantic

- **Performance-critical tight loops**: Validation overhead may matter
- **Simple passthrough**: Just forwarding a dict unchanged
- **Dynamic schemas**: Structure changes at runtime
- **Legacy compatibility**: Need exact dict behavior for external API

In these cases, consider:

- TypedDict for static typing without validation
- Plain dicts with explicit type hints
- Custom **init** with targeted validation

## Validation

```bash
# Verify model works
pytest tests/test_models.py

# Check serialization round-trip
python -c "
from mymodel import MyModel
m = MyModel(field1='test', field2=42)
json_str = m.model_dump_json()
m2 = MyModel.model_validate_json(json_str)
assert m == m2
"

# Verify mypy still passes
mypy path/to/file.py
```

## Pydantic Features Reference

### Model Validation

```python
Model.model_validate(dict)           # From dict
Model.model_validate_json(str)       # From JSON string
Model.model_validate_strings(dict)   # Coerce strings to types
```

### Serialization

```python
model.model_dump()                   # To dict (Python objects)
model.model_dump(mode="json")        # To JSON-compatible dict
model.model_dump_json()              # To JSON string
model.model_dump(exclude={"field"})  # Exclude fields
model.model_dump(by_alias=True)      # Use aliases
```

### Field Configuration

```python
Field(default=...)                   # Default value
Field(default_factory=...)           # Default factory
Field(alias="external_name")         # For input
Field(serialization_alias="...")     # For output
Field(validation_alias="...")        # For validation
Field(gt=0, le=100)                  # Constraints
```

### Validators

```python
@field_validator('email')
@classmethod
def validate_email(cls, v: str) -> str:
    if '@' not in v:
        raise ValueError('Invalid email')
    return v.lower()
```

## References

- [Pydantic V2 Docs](https://docs.pydantic.dev/latest/)
- [Migration Guide](https://docs.pydantic.dev/latest/migration/)
- [JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/)
- [Performance Tips](https://docs.pydantic.dev/latest/concepts/performance/)
