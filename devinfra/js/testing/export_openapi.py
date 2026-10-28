from __future__ import annotations

import json

from devinfra.js.testing.schema_models import app


def main() -> None:
    print(json.dumps(app.openapi(), indent=2))


if __name__ == "__main__":
    main()
