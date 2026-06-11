---
title: Target Pydantic 2 (no Pydantic 1 fallback)
kind: outcome
---

Code targets Pydantic v2 APIs only. Do not write dual‑support shims or fallbacks to Pydantic v1; do not import from `pydantic.v1`. Prefer v2 idioms for validation, configuration, and serialization.

## Acceptance criteria (checklist)

- Use Pydantic v2 decorators and APIs (`field_validator`, `model_validator`, `computed_field`, `model_dump`, `model_dump_json`, `model_validate`)
- Configuration uses v2 config objects (`ConfigDict` / `SettingsConfigDict`) via `model_config = ...` (not `class Config:`)
- Settings come from `pydantic_settings.BaseSettings` (not `pydantic.BaseSettings`)
- No compatibility code or fallbacks such as `try/except ImportError` switching between v1/v2 or `from pydantic import v1 as pydantic`
- Do not use v1‑only features (`root_validator`, `validator` with classmethod semantics, `parse_obj`, `json()`/`dict()` in places where v2 equivalents exist)

## Positive examples

```python
# Pydantic v2 model with field and model validators
from pydantic import BaseModel, field_validator, model_validator, ConfigDict

class User(BaseModel):
    model_config = ConfigDict(validate_assignment=True, str_strip_whitespace=True)

    name: str
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("invalid email")
        return v

    @model_validator(mode="after")
    def check_name(self) -> "User":
        if not self.name:
            raise ValueError("name required")
        return self

u = User(name="A", email="a@example.com")
payload: dict = u.model_dump()
json_payload: str = u.model_dump_json()
```

```python
# Settings in v2 using pydantic-settings
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_")

    debug: bool = False
    data_dir: str

settings = AppSettings()
```

```python
# Programmatic validation of external data in v2
from pydantic import BaseModel

class Item(BaseModel):
    id: int
    title: str

raw = {"id": 1, "title": "hello"}
item = Item.model_validate(raw)
```

## Negative examples

```python
# Dual-support shim — forbidden
try:
    # v2 path
    from pydantic import BaseModel, field_validator
except ImportError:  # v1 fallback — DO NOT WRITE
    from pydantic import BaseModel, validator as field_validator
```

```python
# v1-only validators — forbidden when targeting v2
from pydantic import BaseModel, root_validator, validator

class User(BaseModel):
    name: str
    email: str

    @validator("email")
    def validate_email(cls, v):  # v1 style
        ...

    @root_validator
    def check_all(cls, values):  # v1 style
        ...
```

```python
# v1 Config and serialization — forbidden when v2 equivalents exist
from pydantic import BaseModel

class User(BaseModel):
    class Config:  # v1 style — use model_config = ConfigDict(...)
        validate_assignment = True

u = User(name="A")
_ = u.json()      # v1 — use model_dump_json()
_ = u.dict()      # prefer model_dump()
```

```python
# Importing the v1 compatibility module — forbidden
from pydantic import v1 as pydantic  # Do not use v1 compat layer
```
