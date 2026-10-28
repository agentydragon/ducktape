from __future__ import annotations

import json

from devinfra.x.schema_codegen.typescript.standalone_models import SearchArguments


def main() -> None:
    schema = SearchArguments.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
