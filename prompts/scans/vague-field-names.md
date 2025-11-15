# Scan: Vague Field Names

## Context
@../shared-context.md

## Overview

Field names should be **explicit and self-documenting**. Vague names like `key`, `id`, `name` require context to understand.

## Pattern: Ambiguous Field Names

### Generic Example

```python
# BAD: What kind of key? What does it identify?
class Response(BaseModel):
    key: str  # Hash? Database key? API key? Cache key?
    id: str   # ID of what? Request? Response? User?
    name: str # Name of what?

# GOOD: Explicit names
class Response(BaseModel):
    cache_key: str      # SHA256 hash for cache lookups
    response_id: str    # OpenAI's response ID
    model_name: str     # Model being used
```

### Real Example from rspcache

```python
# BAD: 'key' is ambiguous
class ResponseRecordModel(BaseModel):
    key: str  # ??? What kind of key?

# GOOD: Explicit purpose
class ResponseRecordModel(BaseModel):
    cache_key: str  # SHA256 hash of request body (used for cache lookups)
```

## Common Vague Names

| Vague | Better Alternatives |
|-------|-------------------|
| `key` | `cache_key`, `api_key`, `lookup_key`, `hash_key` |
| `id` | `user_id`, `request_id`, `session_id`, `transaction_id` |
| `name` | `user_name`, `file_name`, `model_name`, `project_name` |
| `data` | `request_data`, `response_data`, `user_data` |
| `value` | `config_value`, `setting_value`, `threshold_value` |
| `type` | `content_type`, `error_type`, `request_type` |
| `status` | Fine if obvious from context, otherwise `job_status`, `request_status` |

## Detection

```bash
# Find single-word field names that might be vague
rg --type py '^\s+key:\s'
rg --type py '^\s+id:\s'
rg --type py '^\s+name:\s'
rg --type py '^\s+data:\s'
rg --type py '^\s+value:\s'
```

## Fix Strategy

1. **Add prefix/suffix clarifying purpose**:
   ```python
   key → cache_key, api_key, encryption_key
   id → user_id, request_id, session_id
   ```

2. **Add docstring if renaming breaks compatibility**:
   ```python
   class Response(BaseModel):
       """Response record.

       Attributes:
           key: SHA256 hash of request body (used for cache lookups)
       """
       key: str  # Still vague, but at least documented
   ```

3. **Best: Rename and update all usages**:
   ```python
   # Find all usages
   rg "\.key\b"

   # Rename in model
   key → cache_key

   # Update all call sites
   response.key → response.cache_key
   ```

## When Vague Names Are OK

- **Within small, focused scope** where context is obvious:
  ```python
  def get_user_by_id(id: int) -> User:  # OK: function name provides context
      ...
  ```

- **Standard conventions** (e.g., `id` in Django/SQLAlchemy models)
- **Temporary variables** with short lifetime:
  ```python
  for key, value in items:  # OK: loop variable, short scope
      ...
  ```

## Benefits

✅ **Self-documenting** - No need to read docs/comments
✅ **Searchable** - `cache_key` is easier to grep than `key`
✅ **Less ambiguity** - Clear what the field represents
✅ **Better autocomplete** - More specific names group logically

## Examples from rspcache

```python
# ✗ BAD: Renamed from vague name
key: str  # What kind of key?

# ✓ GOOD: Explicit
cache_key: str  # SHA256 hash of request body
api_key: APIKeyModel | None  # Client API key for authentication
response_id: str | None  # OpenAI's response ID (e.g., 'resp_abc123')
```

## References

- [Google Python Style Guide - Naming](https://google.github.io/styleguide/pyguide.html#s3.16-naming)
- [PEP 8 - Descriptive Naming Styles](https://peps.python.org/pep-0008/#descriptive-naming-styles)
