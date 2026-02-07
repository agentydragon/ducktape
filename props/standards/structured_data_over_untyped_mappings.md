---
title: Structured data types over untyped mappings
kind: outcome
---

Code uses structured, typed data models for domain payloads and API surfaces rather than ad‑hoc "bag‑of‑whatever" maps.
Avoid `dict[str, Any]`/`Mapping[str, Any]`, `Record<string, unknown>`, `map[string]any`, etc. for application data; prefer
Pydantic models, dataclasses + TypedDicts, TS interfaces/types, Go structs, Java records/POJOs, with proper (de)serialization.

## Acceptance criteria (checklist)

- No new function parameters/returns are untyped or loosely typed maps for domain data (e.g., `dict[str, Any]`, `Mapping[str, Any]`, `Record<string, unknown>`, `map[string]any`)
- Enumerations: when a field has one of N possible options, use a proper enum — not a bare primitive
  - Python: `enum.StrEnum` (3.11+) for string‑valued enums; plain `Enum` for non‑string values. See [Use StrEnum for string‑valued enums](python/strenum.md)
  - TypeScript: string literal unions (preferred) or `enum` with a runtime schema (e.g., zod) for external input
  - Go: define a named type with `const ( ... iota )` values; add `MarshalJSON/UnmarshalJSON` when serializing
  - Java: `enum` for closed sets
- Define concrete schemas for domain payloads:
  - Python: `pydantic.BaseModel` (preferred) or `TypedDict` for simple shapes; dataclasses when value semantics are desired (add explicit serde at boundaries)
  - TypeScript: `interface`/`type` with a runtime schema (zod/io‑ts) when validating external input
  - Go: `struct` with `json` tags
  - Java: records/POJOs with Jackson/Moshi/Gson annotations as needed
- Validation happens at boundaries: parse external JSON into the structured type (e.g., `Model.model_validate(data)`, `z.parse(data)`, `json.Unmarshal(...)`, `ObjectMapper.readValue(...)`)
- Serialization uses library methods (`model_dump(_json)`, `JSON.stringify(value)`, `json.Marshal`, `ObjectMapper.writeValueAsString`) — do not hand‑assemble nested maps
- Temporary map‑like collections are acceptable for inherently map‑shaped data (e.g., HTTP headers/query params, logging contexts); document invariants and normalize to a model ASAP if they cross module boundaries
- Prefer precise fields over opaque blobs; avoid passing through arbitrary `extra` unless explicitly modeled and justified
- Related: keep types precise and explicit; see [type correctness and specificity](./type-correctness-and-specificity.md) and [forbid dynamic attribute access](python/forbid-dynamic-attrs.md); Python should also [target Pydantic 2](python/pydantic-2.md)

## Positive examples

Python (Pydantic v2 model + StrEnum):

```python
from enum import StrEnum
from pydantic import BaseModel, ConfigDict

class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"

class User(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    email: str
    role: Role = Role.USER

def parse_user(raw: dict) -> User:
    return User.model_validate(raw)

# Serialize for transport
payload: dict = User(id="u1", email="u@example.com", role=Role.ADMIN).model_dump()
```

Python (TypedDict for small, static shapes):

```python
from typing import TypedDict

class Health(TypedDict):
    status: str
    uptime_secs: int

def get_health() -> Health:
    return {"status": "ok", "uptime_secs": 12}
```

TypeScript (interface + literal union + runtime check):

```ts
import { z } from "zod";

type Role = "admin" | "user"; // closed set
export interface User {
  id: string;
  email: string;
  role: Role;
}
export const UserSchema = z.object({
  id: z.string(),
  email: z.string().email(),
  role: z.enum(["admin", "user"]),
});

const user: User = UserSchema.parse(JSON.parse(input));
```

Go (struct + typed enum‑like):

```go
type Role string
const (
    RoleAdmin Role = "admin"
    RoleUser  Role = "user"
)

type User struct {
    ID    string `json:"id"`
    Email string `json:"email"`
    Role  Role   `json:"role"`
}

var u User
_ = json.Unmarshal(data, &u)
```

Java (record + enum):

```java
public enum Role { ADMIN, USER }
public record User(String id, String email, Role role) {}
```

## Negative examples (violations)

Opaque dict returned from core logic:

```python
def load_user() -> dict[str, Any]:  # too loose
    return {"id": uid, "mail": email}  # inconsistent, unvalidated keys
```

Ad‑hoc nested map assembly for transport:

```python
payload = {
    "user": {"id": user.id, "email": user.email},
    "meta": extras,  # bag‑of‑whatever
}
# prefer: payload = Envelope(user=user).model_dump()
```

Using primitives for a closed set (should be an enum):

```python
role: str = "admin"  # should be Role (StrEnum)
```

TypeScript domain shape as Record (no schema):

```ts
function makeUser(): Record<string, unknown> {
  // too loose
  return { id: "u1", email: "u@example.com", role: "admin" };
}
```

Go passing dynamic bags through modules:

```go
func Handle(m map[string]any) error {  // too loose
    // callers and callees disagree on keys/types
    return nil
}
```

Notes

- Use map‑like types only for inherently key/value domains (headers, labels), short‑lived and close to their origin
- When introducing a model on an existing loose interface, convert once at the boundary; avoid churn by bouncing between loose and strict forms inside the same flow
