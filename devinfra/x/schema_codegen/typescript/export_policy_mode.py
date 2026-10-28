from __future__ import annotations

import json

from pydantic import TypeAdapter

from devinfra.x.schema_codegen.typescript.standalone_models import PolicyMode


def main() -> None:
    schema = TypeAdapter(PolicyMode).json_schema()
    schema["title"] = "PolicyMode"
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
