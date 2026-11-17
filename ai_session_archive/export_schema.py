"""Print the session archive API's OpenAPI document for frontend type generation."""

import json

from ai_session_archive.web import create_routes_app


def main() -> None:
    print(json.dumps(create_routes_app().openapi(), indent=2))


if __name__ == "__main__":
    main()
