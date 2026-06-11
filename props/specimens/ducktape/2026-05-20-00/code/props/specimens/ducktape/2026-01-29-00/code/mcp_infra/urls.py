from __future__ import annotations

from pydantic import AnyUrl, TypeAdapter

# Single shared AnyUrl adapter for fast validation/coercion across modules
ANY_URL: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)


def parse_any_url(value: str) -> AnyUrl:
    """Validate and coerce a string into AnyUrl using the shared adapter."""
    return ANY_URL.validate_python(value)


# Internal module; keep imports explicit rather than curating a public API
