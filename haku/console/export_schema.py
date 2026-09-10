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
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from haku.console.app import create_app
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig
from haku.console.identity.authorization import StaticAgentDefinition, fingerprint_static_token
from haku.console.settings import Settings

_SCHEMA_CONSOLE_CONFIG = """\
auto_approval_policies:
  - id: schema-manual-review
    type: never
access_profiles:
  - id: schema
    auto_approval_policy: schema-manual-review
default_access_profile_id: schema
"""


def _placeholder_settings(*, config_file: Path) -> Settings:
    # haku_ui_url and database_url are required; placeholders suffice — only routes/models shape the
    # schema, and create_app never connects to the database.
    return Settings(
        haku_ui_url="about:blank",
        auth_origin="https://auth.invalid",
        database_url=SecretStr("postgresql+asyncpg://placeholder/db"),
        public_base_url="https://haku-console.invalid",
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.invalid/application/o/haku-console/",
            client_id="schema",
            client_secret=SecretStr("placeholder-client-secret"),
            session_secret=SecretStr("placeholder-session-secret"),
        ),
        operator_identity=OperatorIdentityConfig(trust_domain="schema.invalid/authentik-user-id/v1"),
        config_file=config_file,
        max_wait_for_result_ms=60_000,
    )


def console_openapi_document() -> dict[str, Any]:
    """The document the frontend's types are generated from."""
    with TemporaryDirectory(prefix="haku-console-schema-") as directory:
        config_file = Path(directory) / "console.yaml"
        config_file.write_text(_SCHEMA_CONSOLE_CONFIG, encoding="utf-8")
        document: dict[str, Any] = create_app(
            _placeholder_settings(config_file=config_file),
            static_agent_definitions=(
                StaticAgentDefinition(
                    agent_id=UUID("00000000-0000-4000-8000-000000000001"),
                    display_name="Schema Agent",
                    operator_id=UUID("00000000-0000-0000-0000-000000000001"),
                    secret_reference="schema-placeholder",
                    token_fingerprint=fingerprint_static_token("placeholder-token"),
                    access_profile_id="schema",
                ),
            ),
        ).openapi()
    return document


def main() -> None:
    print(json.dumps(console_openapi_document(), indent=2))


if __name__ == "__main__":
    main()
