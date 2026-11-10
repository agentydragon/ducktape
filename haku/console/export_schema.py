"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Captured as ``//haku/console:openapi_schema`` for the frontend's generated ``schema.d.ts``. The
document depends only on route signatures, so this registers the production route graph
(``build_app``) and never constructs settings, stores or the OAuth registry.
"""

from __future__ import annotations

import json
from typing import Any

from haku.console.app import build_app


def console_openapi_document() -> dict[str, Any]:
    """The document the frontend's types are generated from."""
    document: dict[str, Any] = build_app(lifespan=None).openapi()
    return document


def main() -> None:
    print(json.dumps(console_openapi_document(), indent=2))


if __name__ == "__main__":
    main()
