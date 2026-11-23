"""Emit the Augur route graph for frontend Zod/TypeScript generation."""

import json

from finance.augur.api.server import build_app


def main() -> None:
    print(json.dumps(build_app(lifespan=None).openapi(), indent=2))


if __name__ == "__main__":
    main()
