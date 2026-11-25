"""Print the Plaid Spend API's OpenAPI schema for frontend type generation."""

import json

from finance.plaid.spend.app import build_app


def main() -> None:
    print(json.dumps(build_app(lifespan=None).openapi(), indent=2))


if __name__ == "__main__":
    main()
