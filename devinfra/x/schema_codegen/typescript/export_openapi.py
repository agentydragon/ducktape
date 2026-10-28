from __future__ import annotations

import json

from devinfra.x.schema_codegen.typescript.schema_models import app


def main() -> None:
    print(json.dumps(app.openapi(), indent=2))


if __name__ == "__main__":
    main()
