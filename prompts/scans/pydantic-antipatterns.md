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

## Detection

```bash
# Find manual field-by-field model_dump patterns
rg --type py -A5 "def (to_db|to_dict|serialize)" | rg "model_dump.*if.*else"

# Find manual field-by-field validation classmethods
rg --type py -A10 "@classmethod" | rg -B3 "return cls\("
```

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
