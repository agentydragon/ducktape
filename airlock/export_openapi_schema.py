"""Export OpenAPI schema from the operator REST API to stdout.

Used by the js_openapi_schema Bazel macro to generate TypeScript type definitions
for the operator frontend's openapi-fetch client.
"""

from __future__ import annotations

import json

from airlock.coordinator import ActionCoordinator
from airlock.operator_api import create_operator_api


def main() -> None:
    # Dummy coordinator — schema export only needs route definitions, not runtime state.
    app = create_operator_api(
        coordinator=ActionCoordinator(backends={}, predicate=lambda ns, tool, args: None),  # type: ignore[arg-type]
        oidc_issuer="https://example.com",
    )
    schema = app.openapi()
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
