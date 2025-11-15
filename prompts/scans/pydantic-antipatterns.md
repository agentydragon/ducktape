# Scan: Pydantic Antipatterns

## Context
@../shared-context.md

## Pattern: Manual Field-by-Field model_dump

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

## Detection

```bash
# Find manual field-by-field patterns
rg --type py -A5 "def (to_db|to_dict|serialize)" | rg "model_dump.*if.*else"
```

## Fix Strategy

1. **If field names match**: Just use `model_dump(mode="json")`
2. **If enum needs `.value`**: Use `@field_serializer`
3. **If fields need different names**: Rename DB columns or use separate DB model class

## References

- [Pydantic Serialization](https://docs.pydantic.dev/latest/concepts/serialization/)
