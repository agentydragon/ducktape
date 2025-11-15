# Scan: Trivial Forwarder Methods

## Context
@../shared-context.md

## Pattern: Single-Call Methods That Add No Value

### Seen in: adgn/rspcache/models.py

```python
# BAD: Method that just calls model_dump
def to_db_payload(self) -> dict[str, Any]:
    return self.model_dump(mode="json")

# GOOD: Call model_dump directly at use site
snapshot.model_dump(mode="json")
```

This includes:
- Methods that forward to a single Pydantic/library method
- Getters that just access an attribute
- Methods with no additional logic, validation, or transformation

## Detection

```bash
# Find single-line return methods
rg --type py -A1 "def \w+\(self.*\):" | rg -B1 "^\s+return self\.\w+\("

# Find methods that just call model_dump
rg --type py "def.*:$" -A1 | rg -B1 "return.*model_dump"
```

## When Trivial Methods Are OK

- **Interface compliance**: Implementing a Protocol/ABC
- **Future extensibility**: Clear plan to add logic
- **Semantic clarity**: Method name adds domain meaning

## Fix Strategy

1. Remove the method
2. Update callers to use direct access/method call
3. If semantics matter, consider a property or use direct access

## References

- [Python properties](https://docs.python.org/3/library/functions.html#property)
