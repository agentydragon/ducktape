# Scan: Pydantic Antipatterns

## Context
@../shared-context.md

## Pattern Description

Repetitive, manual field-by-field operations on Pydantic models instead of using built-in serialization/validation methods.

## Major Antipattern: Manual model_dump Per Field

### The Problem

```python
# BAD: Manually calling model_dump on each field
def to_db_payload(self) -> dict[str, Any]:
    return {
        "status": self.status.value,
        "response_json": self.response.model_dump(mode="json") if self.response else None,
        "error_json": self.error.model_dump(mode="json") if self.error else None,
        "token_usage_json": self.token_usage.model_dump(mode="json") if self.token_usage else None,
    }
```

This pattern is problematic because:
- **Repetitive**: Same `model_dump(mode="json")` pattern repeated
- **Error-prone**: Easy to forget fields or get types wrong
- **Not DRY**: Logic duplicated across similar models
- **Fragile**: Adding fields requires manual updates

### Better Approaches

#### Option 1: Use model_dump at higher level

```python
# GOOD: Let Pydantic handle nested serialization
class FinalResponseSnapshot(BaseModel):
    status: ResponseStatus
    response: OpenAIResponse | None = None
    error: ErrorPayload | None = None
    token_usage: ResponseUsage | None = None

# Just dump the whole model
payload = snapshot.model_dump(mode="json")
# Pydantic automatically handles nested models!
```

#### Option 2: Custom serializer if you need field name mapping

```python
class FinalResponseSnapshot(BaseModel):
    status: ResponseStatus
    response: OpenAIResponse | None = None
    error: ErrorPayload | None = None
    token_usage: ResponseUsage | None = None

    @field_serializer('status')
    def serialize_status(self, value: ResponseStatus) -> str:
        return value.value

    model_config = ConfigDict(
        # Customize serialization behavior
        ser_json_bytes='base64',
        # Map field names if needed
        alias_generator=...
    )

# Now just:
db_payload = snapshot.model_dump(mode="json", by_alias=True)
```

#### Option 3: If database schema differs, use separate model

```python
# Domain model
class FinalResponseSnapshot(BaseModel):
    status: ResponseStatus
    response: OpenAIResponse | None = None
    error: ErrorPayload | None = None
    token_usage: ResponseUsage | None = None

# Database model with different field names
class ResponseSnapshotDB(BaseModel):
    status: str
    response_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
    token_usage_json: dict[str, Any] | None = None

    @classmethod
    def from_domain(cls, snapshot: FinalResponseSnapshot) -> ResponseSnapshotDB:
        payload = snapshot.model_dump(mode="json")
        return cls(
            status=payload["status"],
            response_json=payload.get("response"),
            error_json=payload.get("error"),
            token_usage_json=payload.get("token_usage"),
        )
```

#### Option 4: Store structured data per column

```python
# If using SQLAlchemy with JSONB columns:
class ResponseSnapshot(Base):
    __tablename__ = "response_snapshots"

    # Store Pydantic models directly as JSONB
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    def from_domain(self, snapshot: FinalResponseSnapshot) -> None:
        # Let SQLAlchemy + Pydantic handle serialization
        self.response = snapshot.response.model_dump(mode="json") if snapshot.response else None
        # Or better: use SQLAlchemy type decorators
```

## Detection Strategy

### Grep Patterns

```bash
# Find manual field-by-field model_dump patterns
rg --type py -A10 "def to_db_payload|def to_dict|def serialize" | \
  rg "model_dump\(mode=" | \
  wc -l  # If > 2 in same function, likely antipattern

# Find functions with multiple conditional model_dump calls
rg --type py "\.model_dump\(mode=\"json\"\).*if.*else None"
```

### AST Analysis

```python
import ast

class ManualSerializerDetector(ast.NodeVisitor):
    def visit_FunctionDef(self, node):
        # Look for functions that return dict literal
        if isinstance(node.body[-1], ast.Return):
            ret_value = node.body[-1].value
            if isinstance(ret_value, ast.Dict):
                # Count model_dump calls in dict values
                dump_calls = sum(
                    1 for value in ret_value.values
                    if self._contains_model_dump(value)
                )
                if dump_calls >= 2:
                    print(f"Potential antipattern in {node.name}")
```

## Other Pydantic Antipatterns

### Manual Validation

```python
# BAD: Manual field validation
def parse_response(data: dict) -> ResponseModel:
    if "id" not in data:
        raise ValueError("Missing id")
    if not isinstance(data["id"], str):
        raise ValueError("Invalid id type")
    # ... etc

# GOOD: Use Pydantic
return ResponseModel.model_validate(data)
```

### Not Using model_copy

```python
# BAD: Manual copying
def update_response(resp: Response, new_status: str) -> Response:
    return Response(
        id=resp.id,
        status=new_status,
        created=resp.created,
        # ... all other fields
    )

# GOOD: Use model_copy
def update_response(resp: Response, new_status: str) -> Response:
    return resp.model_copy(update={"status": new_status})
```

### Not Using ConfigDict

```python
# BAD: Manual exclusion logic
def to_json(self) -> dict:
    result = self.model_dump()
    del result["internal_field"]
    del result["cached_value"]
    return result

# GOOD: Configure in model
class MyModel(BaseModel):
    model_config = ConfigDict(
        exclude={"internal_field", "cached_value"}
    )
```

## Fix Strategy

1. **Read Pydantic docs**: Understand model_dump, ConfigDict, field_serializer
2. **Identify pattern**: Is this manual serialization, validation, or copying?
3. **Choose appropriate Pydantic feature**:
   - Serialization → model_dump with config
   - Validation → model_validate
   - Copying → model_copy
   - Field-level control → field_serializer, field_validator
4. **Test thoroughly**: Pydantic behavior can be subtle

## Validation

```bash
# Check manual model_dump patterns reduced
rg --type py "\.model_dump\(mode=\"json\"\).*if.*else" | wc -l

# Verify tests pass
pytest path/to/tests
```

## Reference

- [Pydantic Serialization Docs](https://docs.pydantic.dev/latest/concepts/serialization/)
- [Pydantic ConfigDict](https://docs.pydantic.dev/latest/api/config/)
- [Field Serializers](https://docs.pydantic.dev/latest/concepts/serialization/#field-serializers)
