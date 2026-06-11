"""Export the Study Casino API OpenAPI schema to stdout.

Builds the real FastAPI app from `app.py` with a stub `SqlStore` so route
handlers are registered with their real request/response models without
needing a live database. `app.openapi()` only inspects route signatures —
handler bodies (which would touch the store) are never invoked here.

The output JSON feeds `//x/study_casino/frontend/lib:schema_zod`,
which runs `@hey-api/openapi-ts --plugins zod` to produce
`lib/api/schema.zod.mjs` for the frontend's fetch-boundary validators.
"""

from __future__ import annotations

import json
from typing import Any, cast

from x.study_casino.app import create_app
from x.study_casino.config import Settings
from x.study_casino.state import WsStateChangedMessage
from x.study_casino.store import SqlStore


class _StubStore:
    """`SqlStore` stand-in whose every method raises. The schema-export
    code path constructs the FastAPI app to scrape `app.openapi()`; routes
    register their signatures but their bodies (which call into the store)
    never run."""

    def __getattr__(self, name: str) -> Any:
        def _unreachable(*_: object, **__: object) -> Any:
            raise RuntimeError(f"schema-only: SqlStore.{name} must not be called")

        return _unreachable


def main() -> None:
    settings = Settings(database_url="stub://schema-only")
    app = create_app(settings, store=cast(SqlStore, _StubStore()))
    schema = app.openapi()
    # FastAPI doesn't emit WebSocket frame schemas — there are no
    # `response_model` annotations on websocket routes. Inject the
    # `/ws` payload model so the frontend can validate incoming frames.
    schema.setdefault("components", {}).setdefault("schemas", {})["WsStateChangedMessage"] = (
        WsStateChangedMessage.model_json_schema(ref_template="#/components/schemas/{model}")
    )
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
