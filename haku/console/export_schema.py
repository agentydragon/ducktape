"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Driven by ``//haku/console/frontend:schema`` (``js_openapi_schema``) to generate
``api/schema.d.ts``. Only route/model definitions are needed; placeholder ``Settings``
suffice and ``app.openapi()`` runs no startup work (the Postgres stores are constructed
lazily, and no connection or migration happens outside ``app.main``). The ``/mcp`` server is
mounted as an opaque ASGI sub-app, so it contributes no routes to the schema — it just needs a
placeholder static-agent credential so ``create_app`` builds.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import SecretStr

from haku.console.app import create_app
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig, Settings
from haku.console.mcp_config import ResolvedStaticAgent


def main() -> None:
    # A placeholder static agent so create_app's require-a-/mcp-credential invariant holds; its token
    # and external user key come from env vars named in the config, so set both to placeholders.
    os.environ.setdefault("HAKU_CONSOLE_SCHEMA_AGENT_TOKEN", "placeholder-token")
    os.environ.setdefault("HAKU_CONSOLE_SCHEMA_AGENT_OPERATOR", "placeholder-operator")
    config_file = Path(tempfile.mkdtemp()) / "console.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "static_agents": [
                    {
                        "agent": "schema",
                        "token_env_var": "HAKU_CONSOLE_SCHEMA_AGENT_TOKEN",
                        "operator_subject_env": "HAKU_CONSOLE_SCHEMA_AGENT_OPERATOR",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
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
        config_file=config_file,
    )
    print(
        json.dumps(
            create_app(
                settings,
                resolved_static_agents=[
                    ResolvedStaticAgent(
                        agent="schema",
                        token=SecretStr("placeholder-token"),
                        operator_id=UUID("00000000-0000-0000-0000-000000000001"),
                    )
                ],
            ).openapi(),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
