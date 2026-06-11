# Scan: API Model Design Antipatterns

## Context

@../shared-context.md

## Pattern 1: Denormalized/Computed Fields in Models

### Seen in: adgn/rspcache/admin_app.py

```python
# BAD: Storing denormalized relationship data as flat fields
class ResponseRecordModel(BaseModel):
    api_key_id: UUID | None
    api_key_name: str | None  # Denormalized from api_key.name!

# GOOD: Nested models matching relationships
class ResponseRecordModel(BaseModel):
    api_key: APIKeyModel | None  # Properly nested

    # Backward compat if needed
    @property
    def api_key_id(self) -> UUID | None:
        """Deprecated: use api_key.id instead."""
        return self.api_key.id if self.api_key else None
```

Issues:

- **Data duplication** - Same info in two places (api_key.name and api_key_name)
- **No type safety** - Flat UUID doesn't tell you what it references
- **Harder to evolve** - Adding new api_key fields requires flattening each one
- **Inconsistent** - Some relationships nested, others flattened

## Pattern 2: Duplicated Data Across Tables

### Seen in: adgn/rspcache (Response.token_usage vs ResponseSnapshot.token_usage)

```python
# BAD: Same data stored in two tables with different types
class Response(Base):
    token_usage: Mapped[dict[str, Any] | None]  # Untyped

class ResponseSnapshot(Base):
    token_usage: Mapped[dict[str, Any] | None]  # Also untyped, duplicated

# GOOD: Single source of truth with proper types
class Response(Base):
    # No token_usage here

class ResponseSnapshot(Base):
    token_usage: Mapped[dict[str, Any] | None]  # Only here

# Pydantic model properly typed
class FinalResponseSnapshot(BaseModel):
    token_usage: ResponseUsage | None  # Properly typed!
```

Issues:

- **Synchronization burden** - Must keep both copies in sync
- **Wasted storage** - Same data stored twice
- **Type confusion** - One is `dict[str, Any]`, other should be `ResponseUsage`

## Pattern 3: `_json` Suffix on DB Columns

### Seen in: adgn/rspcache (request_body_json, token_usage_json)

```python
# BAD: Leaking DB implementation details into naming
class Response(Base):
    request_body_json: Mapped[dict[str, Any]]  # "_json" is redundant

class ResponseRecordModel(BaseModel):
    request_body_json: dict[str, Any]  # API exposes DB detail

# GOOD: Clean names, type system already tells you it's JSON
class Response(Base):
    request_body: Mapped[dict[str, Any]]  # Type + JSONB column = obviously JSON

class ResponseRecordModel(BaseModel):
    request_body: dict[str, Any]  # Clean API field name
```

Issues:

- **Redundant** - `Mapped[dict[str, Any]]` + JSONB already tells you it's JSON
- **Leaky abstraction** - API shouldn't know about DB storage format
- **Breaking changes** - If you change DB storage (e.g., to BSON), API breaks

## Pattern 4: Flattened API Models vs Nested DB Relationships

### Seen in: adgn/rspcache/admin_app.py (before refactor)

```python
# BAD: Flattening nested data with renamed fields
class ResponseRecordModel(BaseModel):
    final_response: dict[str, Any] | None  # snapshot.response renamed
    response_error: dict[str, Any] | None  # snapshot.error renamed
    token_usage: dict[str, Any] | None     # snapshot.token_usage duplicated

# GOOD: Match DB structure with nested models
class ResponseRecordModel(BaseModel):
    snapshot: FinalResponseSnapshot | None

class FinalResponseSnapshot(BaseModel):
    response: OpenAIResponse | None  # Properly typed
    error: ErrorPayload | None       # Properly typed
    token_usage: ResponseUsage | None  # Properly typed
```

Issues:

- **Lost structure** - Relationship between response/error/token_usage is obscured
- **Type loss** - `dict[str, Any]` instead of properly typed models
- **Confusing names** - `final_response` vs `response`, `response_error` vs `error`
- **Harder to evolve** - Adding new snapshot fields requires flattening

## Pattern 5: Untyped Dicts Instead of Pydantic Models

### Seen in: Throughout rspcache

```python
# BAD: Using dict[str, Any] when proper types exist
class Response(Base):
    request_body: Mapped[dict[str, Any]]  # OpenAI request exists!

class ResponseRecordModel(BaseModel):
    token_usage: dict[str, Any] | None  # ResponseUsage exists!

# GOOD: Use proper Pydantic types
class FinalResponseSnapshot(BaseModel):
    response: OpenAIResponse | None     # ✓ Typed
    error: ErrorPayload | None          # ✓ Typed
    token_usage: ResponseUsage | None   # ✓ Typed

# Still TODO: type request_body properly
```

Issues:

- **No validation** - `dict[str, Any]` accepts anything, even malformed data
- **No autocomplete** - Can't navigate fields in IDE
- **Runtime errors** - Typos caught at runtime, not compile time
- **Lost documentation** - Type tells you nothing about structure

## Detection Strategy

**Primary Method**: Manual code reading to identify denormalization and type inconsistencies.

**Why automation is insufficient**:

- Determining if a flat field is "denormalized" requires understanding domain relationships
- Some field name patterns (`_id` + `_name`) are legitimate (not references to other models)
- `_json` suffix might be intentional naming, not just DB leakage
- `dict[str, Any]` is sometimes correct (truly dynamic data)

**Manual analysis required**: For each pattern found, understand:

- Is this truly denormalized data from a relationship?
- Does the API structure match domain relationships?
- Are types intentionally loose or just untyped?

**Discovery aids** (candidates for review):

```bash
# Find potential denormalized fields (ID + name pairs - may be legitimate)
rg --type py "_id.*=.*UUID" -A3 | rg "_name.*="

# Find _json suffixes (may be intentional naming)
rg --type py "_json.*Mapped\[dict"

# Find dict[str, Any] that might benefit from typing
rg --type py "dict\[str, Any\]" --type py | grep -v "# OK"
```

## Fix Strategy

1. **For denormalized fields**:
   - Replace flat fields with nested models
   - Add `@property` getters for backward compat
   - Mark old fields as deprecated in docstrings

2. **For duplicated data**:
   - Identify single source of truth
   - Remove duplicates from other tables
   - Update queries to use relationships

3. **For `_json` suffixes**:
   - Rename DB columns (breaking change, needs migration)
   - Update all queries
   - Type system already indicates JSON storage

4. **For flattened structures**:
   - Group related fields into nested models
   - Match API structure to DB relationships
   - Use proper Pydantic models, not dicts

5. **For untyped dicts**:
   - Find or create proper Pydantic models
   - Use TypeAdapter for existing SDK types
   - Add validation at boundaries

## References

- [Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)
- [Database Normalization](https://en.wikipedia.org/wiki/Database_normalization)
- [API Design Best Practices](https://stackoverflow.blog/2020/03/02/best-practices-for-rest-api-design/)
