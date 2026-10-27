"""Print the History Service's Thread entity wire schema for frontend code generation."""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter

from agentplane.history_service.fold.views import ThreadEntityView


def openapi_document() -> dict[str, Any]:
    entity = TypeAdapter(ThreadEntityView).json_schema(ref_template="#/components/schemas/{model}")
    schemas = entity.pop("$defs", {})
    schemas["ThreadEntityView"] = entity
    return {
        "openapi": "3.1.0",
        "info": {"title": "History Service Thread entity", "version": "1"},
        "paths": {},
        "components": {"schemas": schemas},
    }


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
