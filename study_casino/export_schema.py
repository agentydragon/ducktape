"""Export the Study Casino API OpenAPI schema to stdout.

Builds the real FastAPI app from `app.py` with a stub `SqlStore` so route
handlers are registered with their real request/response models without
needing a live database. `app.openapi()` only inspects route signatures —
handler bodies (which would touch the store) are never invoked here.

The output JSON is a declared OpenAPI artifact consumed by the frontend's
file-input Zod converter and normal TypeScript compilation. The `.ts` source
emits `lib/schema_zod.js` plus declarations for the plain-JavaScript
fetch-boundary consumer.
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import TypeAdapter

from study_casino.app import create_app
from study_casino.config import Settings
from study_casino.state import WsStateChangedMessage
from study_casino.store import SqlStore
from util.json_schema import register_models


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
    register_models(
        schema.setdefault("components", {}).setdefault("schemas", {}),
        [("WsStateChangedMessage", "validation", TypeAdapter(WsStateChangedMessage))],
        ref_template="#/components/schemas/{model}",
    )
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
