from __future__ import annotations

from pydantic import AnyUrl, TypeAdapter

# Single shared AnyUrl adapter for fast validation/coercion across modules
ANY_URL: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)


# Internal module; keep imports explicit rather than curating a public API
