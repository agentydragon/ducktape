# Findings: to_db_payload Antipattern in rspcache

## Current Implementation

### Location
`adgn/src/adgn/rspcache/models.py:62-68`

### The Antipattern
```python
class FinalResponseSnapshot(BaseModel):
    status: ResponseStatus
    response: OpenAIResponse | None = None
    error: ErrorPayload | None = None
    token_usage: ResponseUsage | None = None

    def to_db_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "response_json": self.response.model_dump(mode="json") if self.response else None,
            "error_json": self.error.model_dump(mode="json") if self.error else None,
            "token_usage_json": self.token_usage.model_dump(mode="json") if self.token_usage else None,
        }
```

### Problems
1. **Repetitive**: Same `model_dump(mode="json") if ... else None` pattern 3 times
2. **Manual field mapping**: Has to manually map `response` → `response_json`, etc.
3. **Not using Pydantic features**: Pydantic has built-in serialization aliasing
4. **Fragile**: Adding fields requires manual updates in multiple places

## Root Cause Analysis

The method exists because:
1. **Database schema uses different field names**: `response_json` vs `response`
2. **Need JSON serialization**: Pydantic models need to be serialized for JSONB columns
3. **Enum value extraction**: `status.value` instead of enum object

## Proposed Solutions

### Option 1: Use Pydantic Serialization Aliases (RECOMMENDED)

```python
from pydantic import Field, field_serializer

class FinalResponseSnapshot(BaseModel):
    status: ResponseStatus = Field(serialization_alias="status_str")
    response: OpenAIResponse | None = Field(default=None, serialization_alias="response_json")
    error: ErrorPayload | None = Field(default=None, serialization_alias="error_json")
    token_usage: ResponseUsage | None = Field(default=None, serialization_alias="token_usage_json")

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,  # Allow both names during deserialization
    )

    @field_serializer('status')
    def serialize_status(self, value: ResponseStatus) -> str:
        return value.value

# Usage becomes:
payload = snapshot.model_dump(mode="json", by_alias=True)
# Returns: {"status_str": "complete", "response_json": {...}, ...}
```

**Pros**:
- Pydantic handles serialization automatically
- No manual field-by-field dumping
- Type-safe
- Single source of truth for aliasing

**Cons**:
- Need to update `from_db` to handle both names (already handled by `populate_by_name=True`)
- Field names in code differ from DB (but that's already the case!)

### Option 2: Computed Field

```python
from pydantic import computed_field

class FinalResponseSnapshot(BaseModel):
    status: ResponseStatus
    response: OpenAIResponse | None = None
    error: ErrorPayload | None = None
    token_usage: ResponseUsage | None = None

    @computed_field
    @property
    def db_payload(self) -> dict[str, Any]:
        """Computed field for database serialization."""
        return {
            "status": self.status.value,
            "response_json": self.response.model_dump(mode="json") if self.response else None,
            "error_json": self.error.model_dump(mode="json") if self.error else None,
            "token_usage_json": self.token_usage.model_dump(mode="json") if self.token_usage else None,
        }
```

**Pros**:
- Minimal changes to existing code
- Clear that it's a computed/derived value

**Cons**:
- Still has repetitive manual dumping
- Doesn't leverage Pydantic features
- Not much better than current approach

### Option 3: Custom model_dump with exclude/include

This doesn't work well here because we need field name transformation, not just filtering.

### Option 4: Rename Database Columns

```python
# Migration to rename columns
# response_json -> response
# error_json -> error
# token_usage_json -> token_usage

# Then just:
payload = snapshot.model_dump(mode="json")
```

**Pros**:
- Cleanest long-term solution
- No aliasing needed
- Direct mapping

**Cons**:
- Requires database migration
- Breaking change if clients depend on column names
- More work upfront

## Recommendation

**Short term**: Option 1 (serialization aliases)
- Leverages Pydantic properly
- No database changes needed
- Eliminates manual dumping pattern

**Long term**: Consider Option 4 (rename DB columns)
- Simplest model code
- Requires migration but cleaner architecture

## Implementation Plan for Option 1

1. Add `serialization_alias` to each field
2. Add `@field_serializer` for status enum
3. Replace `to_db_payload()` calls with `model_dump(mode="json", by_alias=True)`
4. Update `from_db` if needed (likely not, due to `populate_by_name=True`)
5. Run tests to verify serialization/deserialization works

## Impact Analysis

### Files to modify:
- `adgn/src/adgn/rspcache/models.py` - Add aliases, remove `to_db_payload`
- `adgn/src/adgn/rspcache/responses_db.py` - Replace calls

### Tests needed:
- Verify `model_dump(by_alias=True)` produces correct keys
- Verify `from_db` still works with both field names
- Integration test for database round-trip

## Related Patterns

This same antipattern may exist elsewhere in the codebase. After fixing here, scan for:
```bash
rg --type py "def to_db|def to_dict|def serialize" | \
  xargs -I {} grep -A10 {} | \
  grep "model_dump.*if.*else"
```
