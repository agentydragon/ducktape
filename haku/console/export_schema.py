"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Driven by ``//haku/console/frontend:schema`` (``js_openapi_schema``) to generate
``api/schema.d.ts``. Only route/model definitions are needed; placeholder ``Settings``
suffice and ``app.openapi()`` runs no startup work (the Postgres stores are constructed
lazily, and no connection or migration happens outside ``app.main``). The ``/mcp`` server is
mounted as an opaque ASGI sub-app, so it contributes no routes to the schema — it just needs a
canonical placeholder static-Agent definition so ``create_app`` builds.
"""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import SecretStr

from haku.console.agents.authorization import StaticAgentDefinition, fingerprint_static_token
from haku.console.app import create_app
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig, Settings


def main() -> None:
    # haku_ui_url and database_url are required; placeholders suffice — only routes/models shape the
    # schema, and create_app never connects to the database.
    settings = Settings(
        haku_ui_url="about:blank",
        database_url=SecretStr("postgresql+psycopg://placeholder/db"),
        public_base_url="https://haku-console.invalid",
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.invalid/application/o/haku-console/",
            client_id="schema",
            client_secret=SecretStr("placeholder-client-secret"),
            session_secret=SecretStr("placeholder-session-secret"),
        ),
        operator_identity=OperatorIdentityConfig(trust_domain="schema.invalid/authentik-user-id/v1"),
    )
    print(
        json.dumps(
            create_app(
                settings,
                static_agent_definitions=(
                    StaticAgentDefinition(
                        agent_id=UUID("00000000-0000-4000-8000-000000000001"),
                        display_name="Schema Agent",
                        operator_id=UUID("00000000-0000-0000-0000-000000000001"),
                        secret_reference="schema-placeholder",
                        token_fingerprint=fingerprint_static_token("placeholder-token"),
                    ),
                ),
            ).openapi(),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
