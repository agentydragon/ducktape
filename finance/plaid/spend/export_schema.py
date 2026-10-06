"""Print the Plaid Spend API's OpenAPI schema for frontend type generation."""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import SecretStr

from finance.plaid.spend.app import create_app
from finance.plaid.spend.service import SpendService
from finance.plaid.spend.settings import SpendSettings


def openapi_document() -> dict[str, Any]:
    """Build route/model schemas without connecting to PostgreSQL or an identity provider."""
    settings = SpendSettings(
        database_url="postgresql://schema-export.invalid/unused",
        api_oidc_issuer="https://schema-export.invalid/",
        api_oidc_client_id="schema-export",
        api_oidc_discovered_issuer="https://schema-export.invalid/",
        api_oidc_jwks_uri="https://schema-export.invalid/jwks.json",
        web_oidc_issuer="https://schema-export.invalid/",
        web_oidc_public_base_url="https://schema-export.invalid",
        web_oidc_client_id=SecretStr("schema-export"),
        web_oidc_client_secret=SecretStr("schema-export"),
        web_oidc_session_secret=SecretStr("schema-export"),
    )
    app = create_app(settings, service=cast(SpendService, object()), include_ui=False)
    return cast(dict[str, Any], app.openapi())


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
