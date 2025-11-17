# Code Review: Typed Stubs Suspicious Typing

**File**: `adgn/src/adgn/mcp/testing/typed_stubs.py:56-80`

## Current Code (Problematic)

```python
def _build_arguments(
    payload: BaseModel | dict[str, object], *, input_model: type[BaseModel] | None, exclude_none: bool, tool_name: str
) -> dict[str, object] | None:
    if input_model is not None and not isinstance(payload, input_model):
        raise TypeError(f"{tool_name} expects {input_model.__name__}, got {type(payload).__name__}")
    # model_dump() returns dict[str, Any] which is compatible with dict[str, object]
    return payload.model_dump(exclude_none=exclude_none) if isinstance(payload, BaseModel) else payload


async def call_tool_typed(
    session: Client,
    name: str,
    payload: BaseModel | dict[str, object],
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
    input_model: type[BaseModel] | None = None,
) -> T_Out:
    """Call an MCP tool with a Pydantic input and parse a Pydantic output.

    Requires structuredContent from the server; raises otherwise.
    """
    args = _build_arguments(payload, input_model=input_model, exclude_none=exclude_none, tool_name=name)
    _result, structured = await _call_structured(session, name, args)
    adapter: TypeAdapter[T_Out] = TypeAdapter(out_type)
    # ... rest of validation
```

## Issues

### 1. **`payload` Should Be Instance of `input_model`**

The user's observation is correct:

> isn't payload actually supposed to be instance of input_model?

**YES**. The logic confirms this:

```python
if input_model is not None and not isinstance(payload, input_model):
    raise TypeError(...)
```

**Problem**: Type system doesn't express this constraint.

**Current**: `payload: BaseModel | dict[str, object]` accepts ANY BaseModel
**Reality**: When `input_model` is provided, `payload` must be instance of `input_model`

### 2. **Accepting `dict | BaseModel` Is Suspiciously Loose Typing**

```python
payload: BaseModel | dict[str, object]
```

**Why suspicious**:
- Either payload is a validated Pydantic model (structured, type-safe)
- Or it's an untyped dict (loose, unsafe)
- **This duality defeats the purpose of typed stubs**

User noted:
> plus accepting dict or pydantic is instance of suspiciously loose typing

**Exactly right**. The function is called "call_tool_**typed**" but accepts untyped dicts.

### 3. **`input_model` Is Redundant When `payload` Is Typed**

If payload is already a `BaseModel` instance, we know its type:
```python
isinstance(payload, SomeInputModel)  # True
type(payload).__name__  # "SomeInputModel"
```

**Why have `input_model` parameter at all?**

The only reason: to validate dict payloads:
```python
if input_model is not None and not isinstance(payload, input_model):
    raise TypeError(...)
```

But this check is **runtime**, not **compile-time**.

### 4. **Type Variables Can Express This Properly**

The function SHOULD be:

```python
T_In = TypeVar("T_In", bound=BaseModel)
T_Out = TypeVar("T_Out")

async def call_tool_typed(
    session: Client,
    name: str,
    payload: T_In,
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
) -> T_Out:
    args = payload.model_dump(exclude_none=exclude_none)
    ...
```

**Benefits**:
- Type checker knows `payload` is a BaseModel (no dict)
- No redundant `input_model` parameter
- `T_In` captured for type checking
- Simpler signature

**Usage**:
```python
# Before (redundant input_model):
result = await call_tool_typed(
    session,
    "my_tool",
    MyInputModel(field="value"),
    MyOutputModel,
    input_model=MyInputModel,  # Redundant! We already passed an instance!
)

# After (clean):
result = await call_tool_typed(
    session,
    "my_tool",
    MyInputModel(field="value"),
    MyOutputModel,
)
```

### 5. **The `dict[str, object]` Path Is Untested/Unsafe**

Looking at the implementation:

```python
return payload.model_dump(exclude_none=exclude_none) if isinstance(payload, BaseModel) else payload
```

If `payload` is a dict:
- No validation happens
- `input_model` check only ensures it's NOT a wrong BaseModel, doesn't validate dict shape
- Dict could have wrong keys, wrong types, etc.

**This is dangerous for a "typed" API**.

## Proposed Fix

### Option 1: Proper Type Variables (Best)

```python
from typing import TypeVar

T_In = TypeVar("T_In", bound=BaseModel)
T_Out = TypeVar("T_Out")

async def call_tool_typed(
    session: Client,
    name: str,
    payload: T_In,
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
) -> T_Out:
    """Call an MCP tool with a Pydantic input and parse a Pydantic output.

    Args:
        session: MCP client session
        name: Tool name
        payload: Pydantic model instance (validated input)
        out_type: Expected output model type
        exclude_none: Whether to exclude None values from serialization

    Returns:
        Validated output model instance

    Raises:
        ValidationError: If output doesn't match out_type
        RuntimeError: If server doesn't return structuredContent
    """
    args = payload.model_dump(exclude_none=exclude_none)
    _result, structured = await _call_structured(session, name, args)
    adapter: TypeAdapter[T_Out] = TypeAdapter(out_type)
    try:
        return adapter.validate_python(structured)
    except ValidationError as e:
        raise ValidationError(f"{name} output validation failed: {e}") from e
```

**Benefits**:
- Type-safe: `payload` must be BaseModel
- No redundant `input_model` parameter
- Clearer intent: "typed" means Pydantic models only
- Simpler implementation: no `isinstance()` branches

**Usage**:
```python
class MyInput(BaseModel):
    query: str

class MyOutput(BaseModel):
    result: int

# Type checker knows:
# - payload must be MyInput (or subclass)
# - return type is MyOutput
output: MyOutput = await call_tool_typed(
    session,
    "search",
    MyInput(query="test"),
    MyOutput,
)
```

### Option 2: Overloads (If Dict Support Is Required)

If there's a legitimate reason to support dicts:

```python
from typing import overload

@overload
async def call_tool_typed(
    session: Client,
    name: str,
    payload: T_In,  # BaseModel instance
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
) -> T_Out: ...

@overload
async def call_tool_typed(
    session: Client,
    name: str,
    payload: dict[str, object],
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
    input_model: type[BaseModel],  # Required for dict path
) -> T_Out: ...

async def call_tool_typed(
    session: Client,
    name: str,
    payload: BaseModel | dict[str, object],
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
    input_model: type[BaseModel] | None = None,
) -> T_Out:
    if isinstance(payload, dict):
        if input_model is None:
            raise ValueError("input_model required when payload is dict")
        # Validate dict against input_model
        validated = input_model.model_validate(payload)
        args = validated.model_dump(exclude_none=exclude_none)
    else:
        if input_model is not None and not isinstance(payload, input_model):
            raise TypeError(f"payload must be {input_model.__name__}, got {type(payload).__name__}")
        args = payload.model_dump(exclude_none=exclude_none)

    _result, structured = await _call_structured(session, name, args)
    ...
```

**Better**: Overloads make the constraints explicit:
- Dict path REQUIRES `input_model`
- BaseModel path FORBIDS `input_model` (it's redundant)

### Option 3: Separate Functions (Clearest)

```python
async def call_tool_typed(
    session: Client,
    name: str,
    payload: T_In,
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
) -> T_Out:
    """Call tool with validated Pydantic input."""
    args = payload.model_dump(exclude_none=exclude_none)
    return await _call_and_validate(session, name, args, out_type)

async def call_tool_untyped(
    session: Client,
    name: str,
    payload: dict[str, object],
    input_model: type[T_In],
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
) -> T_Out:
    """Call tool with unvalidated dict (validates before sending)."""
    validated = input_model.model_validate(payload)
    return await call_tool_typed(session, name, validated, out_type, exclude_none=exclude_none)
```

**Clearest intent**:
- `call_tool_typed`: Type-safe, Pydantic models only
- `call_tool_untyped`: Dict path (validates, then delegates)

## Summary

The current code is "suspiciously loose" because:

1. **`payload: BaseModel | dict` defeats type safety** - "typed" function accepts untyped dicts
2. **`input_model` is redundant** - If payload is BaseModel, we already know its type
3. **Should use TypeVar** - `T_In` bound to BaseModel expresses the constraint properly
4. **Runtime check doesn't help type checker** - `isinstance(payload, input_model)` is runtime, not compile-time
5. **Dict path is unsafe** - No validation, just passes through

**Recommendation**: Use Option 1 (TypeVar) for clean, type-safe API. If dict support is truly needed, use Option 2 (overloads) or Option 3 (separate functions).

## Ensure Prompt Captures

**Pattern to scan for**: Functions accepting `BaseModel | dict[str, Any]` with redundant model type parameters

```python
# BAD: Loose typing with redundant parameter
def process(data: BaseModel | dict, *, model_type: type[BaseModel] | None = None):
    if model_type and not isinstance(data, model_type):
        raise TypeError(...)

# GOOD: Proper TypeVar
T = TypeVar("T", bound=BaseModel)
def process(data: T) -> T:
    ...
```

**Scan**: `rg 'BaseModel \| dict' --type py`

**Upsert to scan prompt**: "Suspiciously loose Pydantic typing"
