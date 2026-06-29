"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Driven by ``//haku/console/frontend:schema`` (``js_openapi_schema``) to generate
``api/schema.d.ts``. Only route/model definitions are needed; default ``Settings()``
(every field optional) suffices and ``app.openapi()`` runs no startup work.
"""

from __future__ import annotations

import json

from haku.console.app import create_app
from haku.console.config import Settings


def main() -> None:
    # haku_ui_url is required; a placeholder suffices — only routes/models shape the schema.
    print(json.dumps(create_app(Settings(haku_ui_url="about:blank")).openapi(), indent=2))


if __name__ == "__main__":
    main()
