"""Emit a small multi-root JSON Schema fixture through Pydantic's stock allocator."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import TypeAdapter

from devinfra.x.pydantic_schema_registration.schema_models import FirstRoot, JsonAndDecimal, SecondRoot


def build_schema() -> dict[str, Any]:
    schemas, definitions = TypeAdapter.json_schemas(
        [
            ("FirstRoot", "validation", TypeAdapter(FirstRoot)),
            ("SecondRoot", "validation", TypeAdapter(SecondRoot)),
            ("JsonAndDecimal", "validation", TypeAdapter(JsonAndDecimal)),
            ("JsonAndDecimal", "serialization", TypeAdapter(JsonAndDecimal)),
        ]
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PydanticSchemaRegistrationRoots",
        "type": "object",
        "properties": {
            "first": schemas[("FirstRoot", "validation")],
            "second": schemas[("SecondRoot", "validation")],
            "validation": schemas[("JsonAndDecimal", "validation")],
            "serialization": schemas[("JsonAndDecimal", "serialization")],
        },
        **definitions,
    }


def main() -> None:
    json.dump(build_schema(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
