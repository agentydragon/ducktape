# Scan: Manual Serialization Patterns That Should Use Pydantic

## Context
@../shared-context.md

## Pattern Description

Code using manual JSON serialization/deserialization, dict construction, and validation instead of leveraging Pydantic's built-in capabilities.

## Examples of Antipatterns

### 1. Manual json.loads with dict validation

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

### Grep Patterns

```bash
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
```

### AST Analysis

```python
import ast

class ManualSerdeDetector(ast.NodeVisitor):
    def visit_ClassDef(self, node):
        # Check if dataclass with manual serialization methods
        has_dataclass = any(
            isinstance(d, ast.Name) and d.id == 'dataclass'
            for d in node.decorator_list
        )
        if has_dataclass:
            methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            if 'to_dict' in methods or 'from_dict' in methods:
                print(f"Dataclass with manual serde: {node.name}")
```

## Examples from This Codebase

### adgn/src/adgn/agent/persist/sqlite.py

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
- Custom __init__ with targeted validation

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
