# Scan: Stringly-Typed Code

## Context

@../shared-context.md

## Overview

**"Strings are evil"** - Prefer `StrEnum` (or library-provided enums) over raw strings for categorical values.

Many libraries (OpenAI SDK, etc.) follow this pattern - USE THEIR ENUMS instead of strings.

## Pattern: String Literals Instead of Enums

### Generic Example

```python
# BAD: Stringly-typed
class Request(BaseModel):
    status: str  # What are valid values? Runtime errors on typos!

def process(request: Request):
    if request.status == "complet":  # Typo! Runtime error
        ...

# GOOD: Use StrEnum
class RequestStatus(StrEnum):
    QUEUED = "queued"
    COMPLETE = "complete"
    ERROR = "error"

class Request(BaseModel):
    status: RequestStatus  # Type-safe, autocomplete works

def process(request: Request):
    if request.status == RequestStatus.COMPLETE:  # ✓ Type-checked
        ...
```

### Using Library Enums

```python
# BAD: Re-implementing what library provides
class ModelType(StrEnum):
    GPT_4 = "gpt-4"
    GPT_4_TURBO = "gpt-4-turbo"

# GOOD: Use library's enum (if it exists)
from openai.types import ChatModel  # Example - check if this exists

# BAD: Using strings for well-known values
status_code: str = "success"  # What are valid values?
error_type: str = "validation_error"  # Easy to typo

# GOOD: Use enums from library
from http import HTTPStatus
status_code: HTTPStatus = HTTPStatus.OK

from openai import OpenAIError
# SDK often has error type enums - use them!
```

## Detection

### Quick Manual Patterns

```bash
# Find string fields that should be enums (common patterns)
rg --type py ": str.*#.*(status|type|kind|mode|state)"

# Find Literal types with multiple options (convert to StrEnum)
rg --type py 'Literal\[.*,.*\]'

# Look for repeated string patterns in code - when you see the same string values
# appearing multiple times, that's a strong signal for enum extraction:
# 1. Find existing enums, then search for string comparisons with those values
# 2. Look for status-like field names with string assignments
# 3. Check if codebase uses certain status values repeatedly

# Example: If you found a Status enum with COMPLETED/SKIPPED values,
# search for places still using strings:
#   rg --type py 'status.*=.*"(completed|skipped)"'
#   rg --type py '== "(completed|skipped)"'
#
# The key is to let the codebase guide you - find what enums exist,
# then search for string literals that should use those enums.
```

### Detection Strategy

**MANDATORY Step 0**: Run string literal and symbol histogram scanner.

- This scan is **required** - do not skip this step
- You **must** read and process the histogram output using your intelligence
- High recall required, high precision NOT required - you determine which are categorical values vs messages
- Review histogram for: repeated literals, literal/symbol overlaps, categorical patterns
- Prevents lazy analysis by forcing examination of ALL string literal patterns

**Tool**: `prompts/scans/scan_string_literals.py` - AST-based scanner for string literals and symbols

**What it finds**:

1. **Literal histogram**: All string literals (< 50 chars) sorted by frequency with file/line locations
2. **Symbol histogram**: All symbol names (classes, functions, variables, fields) sorted by frequency
3. **Overlaps**: String literals matching symbol names (STRONG stringly-typed indicator)
   - Example: literal `"status"` + symbol `status` suggests using Status enum instead of strings
4. **Repeated patterns**: Same string appearing many times → likely needs enum

**Usage**:

```bash
# Run on entire codebase
python prompts/scans/scan_string_literals.py . > string_literals_scan.json

# Pretty-print summary
cat string_literals_scan.json | jq '.summary'

# View most frequent literals
cat string_literals_scan.json | jq '.literal_histogram | to_entries | .[0:20]'

# View overlaps (literals matching symbol names)
cat string_literals_scan.json | jq '.overlaps'

# Find specific literal usage
cat string_literals_scan.json | jq '.literal_histogram["completed"]'
```

**Key insight from overlaps**: When you see both a string literal `"status"` and a symbol name `status` appearing frequently, this strongly suggests the code is using strings where it should use an enum. The overlap section surfaces these cases automatically.

**What to review in histogram:**

1. **High-frequency literals**: Strings appearing 5+ times are enum candidates
2. **Categorical patterns**: status/type/mode/state values
3. **Overlaps**: Literal/symbol matches indicate stringly-typed patterns
4. **Related groups**: Multiple literals that seem related (e.g., "queued", "completed", "failed")

**Process ALL high-frequency output**: Focus on literals appearing 5+ times, use judgment to identify categorical values (not messages/URLs/IDs).

---

**Primary Method**: Manual code reading - understand the domain, look for repeated categorical strings. Read code to understand which strings represent categorical values vs messages/IDs/etc.

**Automated Preprocessing AFTER Step 0** (discovers candidates, NOT definitive):

1. **String Literal Repetition Counter** (AST-based)
   - **Now automated by scan_string_literals.py** - run this tool instead
   - Walk AST extracting Constant nodes with string values
   - Filter: 3-30 chars, alphanumeric (skip URLs/paths/messages)
   - Count occurrences, report strings appearing 5+ times
   - Strong LLM can build from description

2. **String Comparison Detector** (AST-based)
   - Find Compare nodes with Eq operator + string literals
   - Group by compared values
   - Shows which strings are used in conditionals

3. **Categorical Field Analyzer** (AST-based)
   - Find AnnAssign nodes where field name contains: "status", "type", "kind", "mode", "state", "level"
   - Check if annotated as `str`
   - These fields are prime enum candidates

4. **Cross-Reference Existing Enums**

```bash
# Find enums already defined
rg --type py "class \w+\(.*Enum\):" -A5

# For each enum value, search for string literal usage
# If Status has COMPLETED = "completed", search for "completed" strings
rg --type py '"completed"' --glob '!**/enums.py'
```

5. **Assignment Pattern Analysis**

```bash
# Count status/type assignments to find common values
rg --type py "(status|type|kind|mode|state)\s*=\s*\"([^\"]+)\"" -o | sort | uniq -c | sort -rn
```

**Critical Workflow**:

1. Run automated tools → get candidate strings
2. **MANUALLY READ CODE** - understand domain, group related values
3. Determine if candidates are truly categorical (not messages/IDs)
4. Create enums for related value groups
5. Search codebase for specific enum values to replace

**Warning**: Automated tools are preprocessing only. String "error" appears everywhere - manual judgment determines if it's an enum candidate or just a message.

## Fix Strategy

1. **Check external API/SDK types FIRST** (Highest Priority):

   **If parsing external API responses** (OpenAI, Anthropic, GitHub, etc.):
   - **CRITICAL**: Check if official SDK exists and provides typed models
   - Use SDK types instead of defining your own string-typed versions
   - Common SDKs with good types:
     - `anthropic` - Anthropic Claude API (`Message`, `ContentBlock`, `TextBlock`, `ToolUseBlock`)
     - `openai` - OpenAI API (`ChatCompletion`, `ChatCompletionMessage`)
     - `github` (PyGithub) - GitHub API
     - `stripe` - Stripe API
     - `google-cloud-*` - Google Cloud APIs

   **Example - BAD (reinventing the wheel)**:

   ```python
   # Parsing Anthropic API responses with custom types
   class TextContent(BaseModel):
       type: str  # ❌ Should use SDK types
       text: str | None = None

   class MessageContent(BaseModel):
       role: str  # ❌ Should be Literal["user"] | Literal["assistant"] from SDK
       content: str | list[dict[str, Any]]  # ❌ Should use ContentBlock from SDK
   ```

   **Example - GOOD (using SDK types)**:

   ```python
   from anthropic.types import Message, ContentBlock, TextBlock, ToolUseBlock

   # Use SDK's properly-typed models directly
   def process_message(message: Message) -> str:
       # message.role is properly typed as Literal["assistant"]
       # message.content is List[ContentBlock] with proper union types
       for block in message.content:
           if isinstance(block, TextBlock):  # Type-safe!
               return block.text
   ```

   **How to check**:

   ```bash
   # 1. Check if SDK is in requirements
   rg "anthropic|openai|github|stripe" requirements.txt pyproject.toml

   # 2. Check what types the SDK provides
   python -c "import anthropic.types; print(dir(anthropic.types))"

   # 3. Read SDK source code (GitHub)
   # Example: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/

   # 4. If SDK exists but you're using custom types, migrate to SDK types
   ```

   **Special case - Internal/Extended formats**:
   - If parsing data that's _based on_ external API but with extensions (e.g., Claude Code history logs)
   - Check if structure matches SDK types closely
   - If mostly matches: Consider using SDK types as base, extend if needed
   - If significantly different: Custom types OK, but use Literal discriminators

   **Example - Claude Code history format**:

   ```python
   # Claude Code logs wrap Anthropic Messages API format:
   # {
   #   "type": "user" | "assistant" | "summary",  # Claude Code-specific
   #   "sessionId": "...", "uuid": "...", "cwd": "...",  # Claude Code metadata
   #   "message": {  # ← This is Anthropic Messages API format!
   #     "role": "user" | "assistant",
   #     "content": [...],  # List[ContentBlock] for assistant
   #     "id": "msg_...",
   #     "model": "claude-...",
   #     "usage": {...}
   #   }
   # }

   # GOOD: Reuse SDK types for the nested message field
   from anthropic.types import Message
   from typing_extensions import Literal

   class ClaudeCodeEntry(BaseModel):
       type: Literal["user", "assistant", "summary"]
       session_id: str = Field(alias="sessionId")
       uuid: str
       timestamp: str
       cwd: str | None = None
       message: Message | None = None  # ← Use SDK type!

   # BAD: Reinventing Message structure
   class ClaudeCodeEntry(BaseModel):
       type: str  # ❌ Should use Literal
       message: dict[str, Any]  # ❌ Loses type safety, SDK types already exist!
   ```

2. **Check standard library enums**:

   ```python
   # Use stdlib enums when available
   from http import HTTPStatus  # For HTTP status codes
   from enum import IntEnum, StrEnum  # For custom enums
   ```

3. **Create StrEnum for internal values**:

   ```python
   from enum import StrEnum

   class MyStatus(StrEnum):
       QUEUED = "queued"
       COMPLETE = "complete"
   ```

4. **Replace string fields**:

   ```python
   # Before
   status: str

   # After
   status: MyStatus
   ```

5. **Update comparisons**:

   ```python
   # Before
   if status == "complete":

   # After
   if status == MyStatus.COMPLETE:
   ```

6. **Serialization handled automatically**:
   - Pydantic serializes `StrEnum` to string in JSON
   - Deserializes string back to `StrEnum`
   - Use `@field_serializer` if you need `.value`

## Benefits

✅ **Type safety** - Typos caught at type-check time, not runtime
✅ **Autocomplete** - IDE shows all valid values
✅ **Documentation** - Enum definition documents all possible values
✅ **Refactoring** - Rename enum value, all usages update
✅ **Exhaustiveness** - Type checker ensures you handle all cases

## Examples from rspcache

```python
# ✓ GOOD: Using StrEnum for internal status
class ResponseStatus(StrEnum):
    COMPLETE = "complete"
    ERROR = "error"

# ✓ GOOD: Using library types
from openai.types.responses import (
    Response as OpenAIResponse,
    ResponseUsage,
    ResponseError,
)

# TODO: Check if OpenAI SDK has status enums we should use
```

## Pattern: Unstructured Error Messages

Error reason/message fields storing free-form strings should use structured types:

```python
# BAD: Free-form error strings
class Response(BaseModel):
    status_reason: str | None = None  # Could be anything!

# Usage scattered across codebase:
status_reason = "Streaming proxy failure"
status_reason = str(exc)  # Exception message
status_reason = f"Upstream status {resp.status_code}"
status_reason = "Upstream returned non-JSON response"

# GOOD: Structured error with StrEnum
class ProxyErrorType(StrEnum):
    UPSTREAM_HTTP = "upstream_http"
    STREAMING_FAILURE = "streaming_failure"
    REQUEST_EXCEPTION = "request_exception"
    INVALID_RESPONSE = "invalid_response"

class ProxyError(BaseModel):
    type: ProxyErrorType
    message: str
    detail: dict[str, Any] | None = None

class Response(BaseModel):
    error: ProxyError | None = None

# BETTER: Tagged union for type-specific fields
class UpstreamHttpError(BaseModel):
    type: Literal["upstream_http"]
    status_code: int
    response_body: str | None = None

class StreamingFailure(BaseModel):
    type: Literal["streaming_failure"]
    exception_message: str

ProxyError = UpstreamHttpError | StreamingFailure | ...
```

**Why structured errors?**

- Categorize errors for metrics/alerting
- Type-safe error handling
- Extract structured info (status codes, etc.)
- Query/filter errors in DB by type

## Common Enum-Worthy Patterns

These string patterns often indicate enum candidates:

- **Status/State**: `"pending"`, `"active"`, `"completed"`, `"failed"`
- **Type/Kind**: `"user"`, `"admin"`, `"system"`
- **Mode**: `"readonly"`, `"readwrite"`, `"admin"`
- **Level**: `"debug"`, `"info"`, `"warning"`, `"error"`
- **Direction**: `"inbound"`, `"outbound"`
- **Format**: `"json"`, `"xml"`, `"csv"`
- **Error reasons**: Multiple different error messages → categorize with enum

## References

- [Python StrEnum docs](https://docs.python.org/3/library/enum.html#enum.StrEnum)
- [Stringly-typed (Martin Fowler)](https://martinfowler.com/bliki/StringlyTyped.html)
- [Pydantic Enums](https://docs.pydantic.dev/latest/concepts/types/#enums)
