# Scan: Library Type Misuse

## Context

@../shared-context.md

## Pattern Description

Using casts, hasattr, getattr, or other dynamic patterns when the library actually provides proper static types. Often caused by not reading the actual library source code.

## The Core Problem

**Assumption**: "The library probably doesn't have good types, so I'll cast/check at runtime"
**Reality**: Modern Python libraries (OpenAI SDK, Pydantic, etc.) have excellent type annotations

## Examples of Misuse

### OpenAI SDK - Unnecessarily Defensive

```python
# BAD: Assumes poor typing
from openai.types.responses import Response

def get_response_data(response: Any) -> dict:
    if hasattr(response, 'model_dump'):
        return response.model_dump(mode="json")
    elif isinstance(response, dict):
        return response
    else:
        return {"error": "unknown type"}

# GOOD: Trust the SDK types
def get_response_data(response: Response) -> dict[str, Any]:
    return response.model_dump(mode="json")
```

### Pydantic - Casting model_dump

```python
# BAD: Unnecessary cast
from pydantic import BaseModel

class MyModel(BaseModel):
    name: str

def serialize(model: MyModel) -> dict[str, Any]:
    # model_dump already returns dict[str, Any]!
    return cast(dict[str, Any], model.model_dump(mode="json"))

# GOOD: Read the source
def serialize(model: MyModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
```

### SQLAlchemy - Poor ORM Type Usage

```python
# BAD: Runtime checks on typed relationships
class User(Base):
    posts: Mapped[list[Post]] = relationship()

def get_post_titles(user: User) -> list[str]:
    if hasattr(user, 'posts'):
        if isinstance(user.posts, list):
            return [p.title for p in user.posts]
    return []

# GOOD: Trust the relationship typing
def get_post_titles(user: User) -> list[str]:
    return [p.title for p in user.posts]
```

## How to Fix: Read The Source

### Step 1: Find the Library Source

```python
import openai
import pydantic
print(openai.__file__)   # /path/to/site-packages/openai/__init__.py
print(pydantic.__file__)  # /path/to/site-packages/pydantic/__init__.py
```

### Step 2: Find Type Definitions

```bash
# Look for .pyi stub files (type hints)
find /path/to/site-packages/openai -name "*.pyi"

# Or look at actual source
cat /path/to/site-packages/openai/types/responses.py
```

### Step 3: Read the Actual Type

```python
# From openai/types/responses.py
class Response(BaseModel):
    """OpenAI Response object."""

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] = "python",
        # ...
    ) -> dict[str, Any]:  # <-- Returns dict[str, Any]
        ...
```

**Conclusion**: `model_dump(mode="json")` already returns `dict[str, Any]`, no cast needed!

## Common Libraries with Good Types

### Excellent Typing (use confidently)

- **OpenAI SDK** (`openai`): Response, Message types
- **Pydantic** (v2): BaseModel, TypeAdapter, validators
- **httpx**: Client, Response, Request
- **FastAPI**: Depends, Request, Response

### Good Typing with Caveats

- **SQLAlchemy**: ORM relationships may need type stubs improvement
- **asyncpg**: Connection, Record types are well-typed
- **Click**: Decorators work well with type hints

### Known Typing Issues

- **pygit2**: Minimal type annotations (consider type stubs)
- **Some older SQLAlchemy patterns**: May need casts for legacy code

## Detection Strategy

**Primary Method**: Manual code reading combined with reading library source code.

**Why automation is insufficient**:

- Determining if a cast/check is "misuse" requires understanding library capabilities
- Need to read actual library source (`.pyi` files or implementation) to verify types
- Some defensive code exists for valid reasons (handling multiple library versions, gradual migration)
- Context matters: is this legacy code during migration or new antipattern?

**Research-based workflow**:

1. Find casts/defensive patterns using grep (discovery)
2. For each, manually research library types
3. Read actual `.pyi` or source files
4. Verify if defensive code is actually needed

**Discovery aids** (candidates for manual verification):

### Grep Patterns

```bash
# Cast on common well-typed libraries (may be legitimate)
rg --type py "cast.*\b(model_dump|Response|httpx|pydantic)\b"

# hasattr on typed objects (often unnecessary but verify)
rg --type py "hasattr\(.*,\s*['\"]model_dump|hasattr\(.*,\s*['\"]response"

# isinstance checks on already-typed variables
rg --type py "isinstance\(response,.*Response\)"
```

### Verification Process (Manual)

For each candidate found:

1. **Find library source**: `import lib; print(lib.__file__)`
2. **Read actual types**: Check `.pyi` files or source
3. **Verify necessity**: Is cast/check actually needed given library types?
4. **Check context**: Legacy migration code or new antipattern?
5. **Remove if unnecessary**: Trust the library types

## Example Investigation

```python
# Found this code:
result = cast(dict[str, Any], snapshot.response.model_dump(mode="json"))

# Investigation:
# 1. Find source
import openai
print(openai.types.responses.__file__)

# 2. Read Response class
# openai/types/responses.py shows:
#   def model_dump(self, *, mode: ...) -> dict[str, Any]

# 3. Conclusion: Cast unnecessary
result = snapshot.response.model_dump(mode="json")
```

## Improving Type Stubs

If you find a library with genuinely poor types:

### Option 1: Inline Type Stubs

```python
# my_project/stubs/pygit2.pyi
class Repository:
    def lookup_branch(self, name: str) -> Branch | None: ...

class Branch:
    name: str
```

Configure mypy:

```ini
# mypy.ini
[mypy]
mypy_path = $MYPY_CONFIG_FILE_DIR/stubs
```

### Option 2: Check for Community Stubs

```bash
# Search for type stubs
pip search types-pygit2
pip install types-sqlalchemy  # Official stubs often exist
```

### Option 3: Contribute to typeshed

- Fork <https://github.com/python/typeshed>
- Add stubs for the library
- Submit PR to help everyone

## Validation

```bash
# Check if cast removal breaks mypy
mypy --strict path/to/file.py

# Use reveal_type to verify inference
# Add this temporarily:
reveal_type(response.model_dump(mode="json"))
# mypy will show: Revealed type is "dict[str, Any]"
```

## When Casts/Checks Are Acceptable

```python
# OK: Library genuinely has poor types
import pygit2  # No proper type stubs
repo: Any = pygit2.Repository(path)
branches = cast(list[str], repo.listall_branches())

# OK: Protocol matching complex structural types
def process_dumpable(obj: SupportsDump) -> dict:
    # cast may be needed for protocol compliance
    return cast(dict[str, Any], obj.model_dump())

# OK: Gradual migration, with TODO
# TODO(typing): Remove cast once SQLAlchemy stubs improve
users = cast(list[User], session.query(User).all())
```

Always document WHY:

```python
# Cast needed: pygit2 lacks type stubs (as of 2024)
branches = cast(list[str], repo.listall_branches())
```

## Reference Links

- OpenAI Python SDK: <https://github.com/openai/openai-python>
- Pydantic types: <https://docs.pydantic.dev/latest/concepts/types/>
- typeshed (Python type stubs): <https://github.com/python/typeshed>
- mypy reveal_type: <https://mypy.readthedocs.io/en/stable/common_issues.html#reveal-type>
