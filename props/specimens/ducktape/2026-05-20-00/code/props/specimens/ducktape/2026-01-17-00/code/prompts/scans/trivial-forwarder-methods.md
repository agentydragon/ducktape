# Scan: Trivial Forwarder Methods

## Context

@../shared-context.md

## Pattern Description

Class methods that do nothing but forward to another method or function with identical or trivially transformed arguments. Similar to trivial-forwarders.md but for methods specifically.

## Examples

```python
# BAD: Trivial method forwarder
class DataProcessor:
    def process(self, data: dict) -> Result:
        return self._process_impl(data)  # Just forwards

# BAD: Property that just returns attribute
class Config:
    @property
    def value(self) -> str:
        return self._value  # No transformation, just forwarding

# GOOD: Method adds value (validation, logging, transformation)
class DataProcessor:
    def process(self, data: dict) -> Result:
        logger.info(f"Processing {len(data)} items")
        return self._process_impl(data)
```

## Detection Strategy

**Primary Method**: Manual code reading to determine if method adds semantic value.

**Why automation is insufficient**:

- "Trivial" depends on architectural intent (facade pattern, interface compliance)
- Some forwarders exist for valid reasons (API stability, dependency injection points)
- Properties vs direct attributes often intentional (future extensibility)

**Manual review required**: Understand why method exists before removing.

## References

- See also: `trivial-forwarders.md` for function-level forwarders
