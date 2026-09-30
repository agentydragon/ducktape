"""Print the pairing page API's OpenAPI schema to stdout (for frontend type-gen).

Driven by `//devinfra/claude/session_export/frontend:schema` (`js_openapi_schema`) so the page's wire types are
the API's Pydantic models rather than a hand-kept copy. `app.openapi()` reads route signatures only: nothing
here connects to a database, an IdP or Anthropic, and the placeholders exist because `create_app` requires them.
"""

import json
from pathlib import Path

import httpx

from devinfra.claude.session_export.settings import ServeSettings
from devinfra.claude.session_export.store import SessionStore, make_engine
from devinfra.claude.session_export.supervisor import SyncSupervisor
from devinfra.claude.session_export.web import create_app


def openapi_document() -> dict[str, object]:
    settings = ServeSettings(
        database_url="postgresql://schema-export.invalid/unused",
        credentials_file=Path("/nonexistent/credentials.json"),
        public_base_url="https://schema-export.invalid",
        oidc_issuer="https://schema-export.invalid/",
        oidc_client_id="schema-export",
        oidc_client_secret="schema-export",
        oidc_session_secret="schema-export",
        oidc_allowed_subject="schema-export",
    )
    store = SessionStore(make_engine(settings.database_url))
    supervisor = SyncSupervisor.for_settings(settings, store=store, token_client=httpx.AsyncClient())
    document: dict[str, object] = create_app(supervisor=supervisor, settings=settings, store=store).openapi()
    return document


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
