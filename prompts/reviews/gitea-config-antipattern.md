# Code Review: Gitea Mirror Server Config Anti-Pattern

**File**: `adgn/src/adgn/mcp/gitea_mirror/server.py:232-238`

## Current Code (Bad)

```python
def make_gitea_mirror_server(*, base_url: str | None = None, token: str | None = None) -> NotifyingFastMCP:
    cfg = MirrorConfig(
        base_url=str(base_url or os.environ.get("GITEA_BASE_URL", "")),
        token=str(token or os.environ.get("GITEA_TOKEN", "")),
    )
    if not cfg.base_url or not cfg.token:
        raise ValueError("Gitea mirror MCP requires GITEA_BASE_URL and GITEA_TOKEN")
```

## Why This Is Bad

### 1. **`str(None)` Produces `"None"` String**

```python
>>> str(None or os.environ.get("NONEXISTENT", ""))
''
>>> str(None)
'None'
```

If `base_url=None` and `GITEA_BASE_URL` is not set:
```python
str(None or "")  # → str("") → ""  ✓ OK
```

But if someone calls `make_gitea_mirror_server(base_url="", token=None)`:
```python
str("" or os.environ.get("GITEA_BASE_URL", ""))  # → str("") → ""  ✓ OK (falsy "" skips to env)
```

Actually wait, the issue is more subtle:

```python
# If base_url=None, GITEA_BASE_URL not set:
base_url or os.environ.get("GITEA_BASE_URL", "")
→ None or ""
→ ""
str("")  → ""  ✓ OK

# If base_url="some_url":
str(base_url or ...)
→ str("some_url")  ✓ OK

# If base_url=None, GITEA_BASE_URL="http://gitea":
str(None or "http://gitea")
→ str("http://gitea")  ✓ OK
```

Hmm, actually the `str()` wrapper is harmless here but **pointless**. The real issues:

### 2. **Empty String `""` Passes Through, Caught Later**

```python
cfg = MirrorConfig(base_url="", token="")  # Allowed by dataclass
if not cfg.base_url or not cfg.token:  # Caught here
    raise ValueError(...)
```

**Why bad**: The `MirrorConfig` dataclass accepts invalid state, validation happens externally.

**Better**: Validate at construction:
```python
@dataclass
class MirrorConfig:
    base_url: str
    token: str

    def __post_init__(self):
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.token:
            raise ValueError("token is required")
```

Or use Pydantic with validators:
```python
class MirrorConfig(BaseModel):
    base_url: str = Field(min_length=1)
    token: str = Field(min_length=1)
```

### 3. **Confusing Fallback Logic with `str()` Wrapper**

The `str(x or y)` pattern suggests the values might not be strings, but:
- `base_url: str | None` → already string or None
- `os.environ.get("GITEA_BASE_URL", "")` → already returns string
- `str()` wrapper is **redundant** and **confusing**

```python
# What the code does:
str(base_url or os.environ.get("GITEA_BASE_URL", ""))

# What it should do:
base_url or os.environ.get("GITEA_BASE_URL") or ""

# Or more explicit:
base_url if base_url is not None else os.environ.get("GITEA_BASE_URL", "")
```

### 4. **Silent Failure Mode: Empty String Credentials**

If environment variables are set to empty strings:
```bash
export GITEA_BASE_URL=""
export GITEA_TOKEN=""
```

```python
os.environ.get("GITEA_BASE_URL", "")  # → ""
```

The `or` chain doesn't help because `""` is falsy:
```python
None or ""  # → ""
"" or ""    # → ""
```

But the code DOES catch this:
```python
if not cfg.base_url or not cfg.token:
    raise ValueError(...)
```

**Problem**: The error happens AFTER instantiation, not during.

### 5. **Type Confusion: Parameters Accept None, But Immediately Converted**

```python
def make_gitea_mirror_server(*, base_url: str | None = None, ...)
```

**Signature says**: "I accept None"
**Implementation does**: "I immediately convert None to empty string, then reject empty strings"

**Why bad**: The `| None` type is a lie. The function never meaningfully uses None; it's just a sentinel for "use environment variable".

**Better signature**:
```python
def make_gitea_mirror_server(
    *,
    base_url: str = "",  # Default to empty, fallback to env
    token: str = "",
) -> NotifyingFastMCP:
    final_base_url = base_url or os.environ.get("GITEA_BASE_URL", "")
    final_token = token or os.environ.get("GITEA_TOKEN", "")

    if not final_base_url:
        raise ValueError("GITEA_BASE_URL required (pass base_url= or set env)")
    if not final_token:
        raise ValueError("GITEA_TOKEN required (pass token= or set env)")

    cfg = MirrorConfig(base_url=final_base_url, token=final_token)
    ...
```

Or even better, make env lookup explicit:

```python
def make_gitea_mirror_server(
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> NotifyingFastMCP:
    """Create Gitea mirror server.

    Args:
        base_url: Gitea base URL. If None, reads from GITEA_BASE_URL env var.
        token: Gitea access token. If None, reads from GITEA_TOKEN env var.
    """
    final_base_url = base_url if base_url is not None else os.getenv("GITEA_BASE_URL")
    final_token = token if token is not None else os.getenv("GITEA_TOKEN")

    if not final_base_url:
        raise ValueError("base_url required: pass base_url= or set GITEA_BASE_URL")
    if not final_token:
        raise ValueError("token required: pass token= or set GITEA_TOKEN")

    cfg = MirrorConfig(base_url=final_base_url, token=final_token)
    ...
```

### 6. **The Real Anti-Pattern: Delayed Validation**

```python
cfg = MirrorConfig(...)  # Accepts invalid state
if not cfg.base_url:     # Validate later
    raise ValueError(...) # Manually
```

**Core issue**: Separating instantiation from validation allows invalid objects to exist.

**Principle**: Make invalid states unrepresentable.

## Fixed Version

```python
from pydantic import BaseModel, Field, field_validator

class MirrorConfig(BaseModel):
    base_url: str = Field(min_length=1, description="Gitea instance base URL")
    token: str = Field(min_length=1, description="Gitea access token")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        if not v:
            raise ValueError("base_url cannot be empty")
        return v.rstrip("/")  # Normalize trailing slash

def make_gitea_mirror_server(
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> NotifyingFastMCP:
    """Create Gitea mirror server.

    Falls back to GITEA_BASE_URL and GITEA_TOKEN environment variables
    if parameters are not provided.
    """
    resolved_base_url = base_url if base_url is not None else os.getenv("GITEA_BASE_URL")
    resolved_token = token if token is not None else os.getenv("GITEA_TOKEN")

    if not resolved_base_url:
        raise ValueError(
            "Gitea base URL required: pass base_url= or set GITEA_BASE_URL environment variable"
        )
    if not resolved_token:
        raise ValueError(
            "Gitea token required: pass token= or set GITEA_TOKEN environment variable"
        )

    # MirrorConfig validates non-empty strings at construction
    cfg = MirrorConfig(base_url=resolved_base_url, token=resolved_token)
    ...
```

## Summary

The original code is bad because:

1. **Redundant `str()` wrapper**: Values are already strings
2. **`| None` type hint misleading**: None is never meaningfully used, just a sentinel
3. **Validation happens after instantiation**: `MirrorConfig` can exist in invalid state
4. **Error messages are vague**: Don't explain env var fallback
5. **Unclear intent**: Is `or` for None handling or empty string handling? (Both, confusingly)

**Fix**:
- Use `is not None` checks for explicit env fallback
- Validate at construction (Pydantic models with `Field(min_length=1)`)
- Provide clear error messages with remediation steps
- Remove pointless `str()` wrappers
