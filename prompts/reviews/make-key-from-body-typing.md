# Code Review: make_key_from_body Should Take Pydantic Model

**File**: `adgn/src/adgn/rspcache/__init__.py:110-114`

## Current Code

```python
def make_key_from_body(body: dict[str, Any]) -> str:
    keyed = {
        k: body[k] for k in sorted(body.keys()) if k not in {"request_id", "request_timestamp", "nonce", "__meta__"}
    }
    return hashlib.sha256(canonicaljson.encode_canonical_json(keyed)).hexdigest()
```

## Problem

The function accepts `dict[str, Any]` but is used to create cache keys for OpenAI API requests, which have a well-defined Pydantic model structure.

**Issues**:

1. **Type Safety Lost**: `dict[str, Any]` accepts any dictionary, no validation
2. **No IDE Support**: No auto-completion for valid request fields
3. **Runtime Errors Possible**: Typos in field names not caught until runtime
4. **Unclear Contract**: What fields are required? Optional? What are their types?

## Proposed Fix

### Option 1: Accept Pydantic Model

```python
from openai.types.responses import ResponseCreateParams  # Or appropriate request type

def make_key_from_body(body: ResponseCreateParams) -> str:
    """Create cache key from OpenAI request body.

    Excludes non-deterministic fields: request_id, request_timestamp, nonce, __meta__
    """
    body_dict = body.model_dump(mode="json", exclude_none=True)
    keyed = {
        k: v for k, v in body_dict.items()
        if k not in {"request_id", "request_timestamp", "nonce", "__meta__"}
    }
    return hashlib.sha256(canonicaljson.encode_canonical_json(keyed)).hexdigest()
```

**Benefits**:
- Type-safe: Only valid request models accepted
- IDE support: Auto-completion, type checking
- Self-documenting: Clear what type of body this operates on
- Validation: Pydantic validates structure before cache key generation

### Option 2: Use TypedDict (If Pydantic Model Not Available)

```python
from typing import TypedDict, NotRequired

class CacheableRequestBody(TypedDict):
    model: str
    input: list[dict[str, Any]]
    # ... other required fields
    request_id: NotRequired[str]  # Excluded from cache key
    request_timestamp: NotRequired[str]  # Excluded from cache key
    nonce: NotRequired[str]  # Excluded from cache key

def make_key_from_body(body: CacheableRequestBody) -> str:
    keyed = {
        k: v for k, v in body.items()
        if k not in {"request_id", "request_timestamp", "nonce", "__meta__"}
    }
    return hashlib.sha256(canonicaljson.encode_canonical_json(keyed)).hexdigest()
```

**Benefits**:
- Type checking without Pydantic dependency
- Clear which fields are expected
- Still accepts dicts (TypedDict is structural typing)

### Option 3: Overload for Both Dict and Pydantic (Compatibility)

```python
from typing import overload, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

@overload
def make_key_from_body(body: T) -> str: ...

@overload
def make_key_from_body(body: dict[str, Any]) -> str: ...

def make_key_from_body(body: BaseModel | dict[str, Any]) -> str:
    """Create cache key from request body (Pydantic model or dict)."""
    if isinstance(body, BaseModel):
        body_dict = body.model_dump(mode="json", exclude_none=True)
    else:
        body_dict = body

    keyed = {
        k: v for k, v in body_dict.items()
        if k not in {"request_id", "request_timestamp", "nonce", "__meta__"}
    }
    return hashlib.sha256(canonicaljson.encode_canonical_json(keyed)).hexdigest()
```

**Benefits**:
- Backwards compatible with existing dict callers
- Type-safe when using Pydantic models
- Gradual migration path

## Recommendation

**Use Option 1 (Pydantic Model)** if:
- All callers already have validated Pydantic models
- rspcache is the only module using this function
- You want strictest type safety

**Use Option 3 (Overload)** if:
- Need backwards compatibility
- Multiple callers, some with dicts, some with models
- Gradual refactoring preferred

## Impact on Callers

Current usage in `responses_endpoint`:
```python
body = await request.json()  # Returns dict[str, Any]
key = make_key_from_body(body)
```

**With Pydantic model**:
```python
from openai.types.responses import ResponseCreateParams

body_dict = await request.json()
body = ResponseCreateParams.model_validate(body_dict)  # Validate early
key = make_key_from_body(body)
```

**Benefits of early validation**:
- Catch malformed requests before caching
- Clear error messages for invalid requests
- Type-safe throughout request handling
- Can reuse validated model (no redundant parsing)

## Summary

The current `make_key_from_body(body: dict[str, Any])` signature is **suspiciously loose typing** for a function that operates on well-defined OpenAI request structures.

**Recommendation**: Accept `ResponseCreateParams` Pydantic model (or appropriate request type) for type safety, IDE support, and early validation.

**Migration**: Add validation step at call site to convert `dict` → `Pydantic model` before passing to `make_key_from_body`.
