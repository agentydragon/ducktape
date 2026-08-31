"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Driven by ``//haku/console/frontend:schema`` (``js_openapi_schema``) to generate
``api/schema.d.ts``. Only route/model definitions are needed; placeholder ``Settings``
suffice and ``app.openapi()`` runs no startup work (the Postgres stores are constructed
lazily, and no connection or migration happens outside ``app.main``). The ``/mcp`` server is
mounted as an opaque ASGI sub-app, so it contributes no routes to the schema — it just needs a
canonical placeholder static-Agent definition so ``create_app`` builds.

**What a socket sends is published here too.** FastAPI documents routes, and a WebSocket is not
one, so nothing a follower receives would appear in this document and the browser would type those
messages by hand — where a field renamed on the server still compiles in the client and breaks the
wire at runtime. `ConversationFollowMessage` is added to ``components.schemas`` so the frontend
generates it like any response body.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from pydantic import SecretStr, TypeAdapter

from haku.console.app import create_app
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig
from haku.console.identity.authorization import StaticAgentDefinition, fingerprint_static_token
from haku.console.session.conversation_views import ConversationFollowMessage
from haku.console.settings import Settings

# The component the follow socket's messages are published under, and what the frontend's
# `ConversationFollowMessage` in `frontend/client.ts` resolves to.
FOLLOW_MESSAGE_SCHEMA = "ConversationFollowMessage"

_REF_TEMPLATE = "#/components/schemas/{model}"

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


def _publish_follow_messages(document: dict[str, Any]) -> None:
    """Add what `WS /api/conversations/{id}/follow` sends to *document*'s component schemas.

    Serialization mode, which is how this document already describes a response body — these are
    sent, never received, and the components they reuse were generated that way.

    **Only the messages themselves may be new.** Every row they carry — the conversation, its
    session's messages and turns, the sandbox view — is one a conversation read already returns, so
    a second definition of any of them would mean this document names that shape twice and the two
    could disagree. Such a name is refused rather than published.
    """
    schemas = document["components"]["schemas"]
    union = TypeAdapter(ConversationFollowMessage).json_schema(mode="serialization", ref_template=_REF_TEMPLATE)
    definitions = union.pop("$defs")
    messages = {str(branch["$ref"]).rsplit("/", 1)[-1] for branch in union["oneOf"]}
    if unknown := set(definitions) - set(schemas) - messages:
        raise ValueError(f"the follow messages carry shapes this document does not define: {sorted(unknown)}")
    schemas[FOLLOW_MESSAGE_SCHEMA] = union
    schemas.update({name: schema for name, schema in definitions.items() if name in messages})


def console_openapi_document() -> dict[str, Any]:
    """The document the frontend's types are generated from: the routes, plus the socket messages."""
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
    _publish_follow_messages(document)
    return document


def main() -> None:
    print(json.dumps(console_openapi_document(), indent=2))


if __name__ == "__main__":
    main()
