"""Print the pairing page API's OpenAPI schema to stdout (for frontend type-gen).

Captured as `//devinfra/claude/session_export:openapi_schema` so the page's wire types are the API's
Pydantic models rather than a hand-kept copy. `app.openapi()` reads route signatures only, so this registers the
production routes (`create_routes`) and attaches none of what serves them: no database, IdP or Anthropic client.
"""

import json

from devinfra.claude.session_export.web import create_routes


def main() -> None:
    print(json.dumps(create_routes().openapi(), indent=2))


if __name__ == "__main__":
    main()
