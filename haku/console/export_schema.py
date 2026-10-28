"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Captured as ``//haku/console:openapi_schema`` for the frontend's generated
``schema.d.ts``. Only route/model definitions are needed; placeholder ``Settings``
suffice and ``app.openapi()`` runs no startup work (the Postgres stores are constructed
lazily, and no connection or migration happens outside ``app.main``).
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import SecretStr

from haku.console.app import create_app
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig
from haku.console.settings import Settings

_SCHEMA_CONSOLE_CONFIG = """\
access_profiles:
  - id: schema
default_access_profile_id: schema
"""


def _placeholder_settings(*, config_file: Path) -> Settings:
    # haku_ui_url and database_url are required; placeholders suffice — only routes/models shape the
    # schema, and create_app never connects to the database.
    return Settings(
        haku_ui_url="about:blank",
        auth_origin="https://auth.invalid",
        database_url=SecretStr("postgresql://placeholder/db"),
        public_base_url="https://haku-console.invalid",
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.invalid/application/o/haku-console/",
            client_id="schema",
            client_secret=SecretStr("placeholder-client-secret"),
            session_secret=SecretStr("placeholder-session-secret"),
        ),
        operator_identity=OperatorIdentityConfig(trust_domain="schema.invalid/authentik-user-id/v1"),
        config_file=config_file,
    )


def console_openapi_document() -> dict[str, Any]:
    """The document the frontend's types are generated from."""
    with TemporaryDirectory(prefix="haku-console-schema-") as directory:
        config_file = Path(directory) / "console.yaml"
        config_file.write_text(_SCHEMA_CONSOLE_CONFIG, encoding="utf-8")
        document: dict[str, Any] = create_app(_placeholder_settings(config_file=config_file)).openapi()
    return document


def main() -> None:
    print(json.dumps(console_openapi_document(), indent=2))


if __name__ == "__main__":
    main()
