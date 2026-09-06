"""Print the aiquota API's OpenAPI schema to stdout (for frontend type-gen).

Driven by ``//aiquota/frontend:schema`` (``js_openapi_schema``) so the dashboard's
wire types are the API's Pydantic models rather than a hand-kept copy of them.
``app.openapi()`` reads route signatures only: nothing here fetches a provider, and the
placeholder fetcher exists solely because ``create_app`` requires one.
"""

import json

from aiquota.api import QuotaSnapshot, create_app


class _UnusedFetcher:
    """Stands in for the provider fetcher; the schema export serves no request."""

    async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
        raise NotImplementedError("schema export never fetches quotas")


def aiquota_openapi_document() -> dict[str, object]:
    document: dict[str, object] = create_app(bearer_token="schema-placeholder", fetcher=_UnusedFetcher()).openapi()
    return document


def main() -> None:
    print(json.dumps(aiquota_openapi_document(), indent=2))


if __name__ == "__main__":
    main()
