"""Shared fixtures for haku/console tests: a real Postgres (testcontainer) and a TestClient
over the console app wired to it.

Postgres is required by the console (approval ledger + operator OAuth store), so every test that
builds the app runs against a fresh per-test database. `db_url` is a pristine empty database (used by
the migration tests, which drive alembic themselves); `migrated_db_url` is the same database upgraded
to head (used by everything else, including `make_client`).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import itsdangerous
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from haku.console.app import create_app
from haku.console.config import OperatorOidcConfig, Settings
from haku.console.database_migrate import apply_migrations
from haku.console.operator_auth import SESSION_USER_KEY
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database_sync

# A default static agent so `create_app`'s require-a-/mcp-credential invariant is satisfied without
# every test spelling one out — the real deploy always has the `haku` agent. Tests that exercise
# agent auth pass their own `config_file` naming the agents (and bearer) they need.
_DEFAULT_AGENT_TOKEN_ENV = "HAKU_CONSOLE_DEFAULT_AGENT_TOKEN"
_DEFAULT_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_DEFAULT_AGENT_OPERATOR"
_DEFAULT_STATIC_AGENTS = [
    {"agent": "console", "token_env_var": _DEFAULT_AGENT_TOKEN_ENV, "operator_subject_env": _DEFAULT_AGENT_OPERATOR_ENV}
]

# App-owned operator auth for tests. A dummy `operator_oidc` (no live IdP needed) activates
# SessionMiddleware + the router guards exactly as production does; tests inject the operator session
# cookie directly rather than walking the full Authentik login (that end-to-end path is covered by
# test_operator_auth). The retired forward-auth `x-authentik-*` headers are no longer trusted, so a
# session is the only way to present operator identity.
_TEST_SESSION_SECRET = "test-operator-session-secret"
TEST_OPERATOR_OIDC = OperatorOidcConfig(
    issuer="https://auth.test/application/o/haku-console/",
    client_id="console",
    client_secret=SecretStr("client-secret"),
    session_secret=SecretStr(_TEST_SESSION_SECRET),
)


def operator_session_cookie(*, subject: str, username: str) -> str:
    """A Starlette SessionMiddleware `session` cookie for a logged-in operator, mirroring its own
    sign format (`TimestampSigner` over base64-JSON) so a TestClient can present operator identity
    without walking the live OIDC login."""
    data = base64.b64encode(json.dumps({SESSION_USER_KEY: {"subject": subject, "username": username}}).encode())
    return itsdangerous.TimestampSigner(_TEST_SESSION_SECRET).sign(data).decode()


@pytest.fixture(scope="session", autouse=True)
def _preload_postgres_images() -> None:
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Generator[str]:
    """A pristine, empty per-test database (no migrations)."""
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_") or "haku_console_test"
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    yield postgres_admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    force_drop_database_sync(postgres_admin_url, db_name)


@pytest.fixture
def migrated_db_url(db_url: str) -> str:
    """The per-test database upgraded to head — what the app expects at runtime (migrations run as an
    explicit startup step, not inside a store constructor)."""
    apply_migrations(db_url)
    return db_url


@pytest.fixture
def make_client(migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Factory: a TestClient over the console app on a fresh migrated database, with optional
    ``Settings`` overrides (e.g. ``config_file=...``, ``mcp_oauth=...``)."""
    monkeypatch.setenv(_DEFAULT_AGENT_TOKEN_ENV, "default-agent-token")
    monkeypatch.setenv(_DEFAULT_AGENT_OPERATOR_ENV, "default-op")
    default_config = tmp_path / "console_default.yaml"
    default_config.write_text(yaml.safe_dump({"static_agents": _DEFAULT_STATIC_AGENTS}), encoding="utf-8")

    @contextmanager
    def _make(
        *,
        tool_call_executor: Any | None = None,
        tool_call_metadata_provider: Any | None = None,
        gmail_client: Any | None = None,
        calendar_client: Any | None = None,
        in_process_servers: Any | None = None,
        operator: bool = False,
        operator_subject: str = "operator-sub",
        operator_username: str = "operator@example.com",
        **settings_overrides: Any,
    ) -> Iterator[TestClient]:
        # `operator=True` runs the app in the production app-owned-auth mode (SessionMiddleware +
        # active router guards) and presents an authenticated operator session on the client — the
        # replacement for the retired `x-authentik-*` header stand-in.
        if operator and "operator_oidc" not in settings_overrides:
            settings_overrides = {**settings_overrides, "operator_oidc": TEST_OPERATOR_OIDC}
        # haku_ui_url and database_url are required; default them, plus a config_file naming the
        # default static agent (so /mcp has a credential), so callers only override what they test.
        settings = Settings(
            **{
                "haku_ui_url": "https://haku-ui.test",
                "database_url": SecretStr(migrated_db_url),
                "config_file": default_config,
                **settings_overrides,
            }
        )
        app = create_app(settings)
        if tool_call_executor is not None:
            app.state.tool_call_executor = tool_call_executor
        if tool_call_metadata_provider is not None:
            app.state.tool_call_metadata_provider = tool_call_metadata_provider
        if gmail_client is not None:
            app.state.gmail_client = gmail_client
        if calendar_client is not None:
            app.state.calendar_client = calendar_client
        if in_process_servers is not None:
            app.state.in_process_servers = in_process_servers
        # When the session cookie is Secure (https public_base_url → https_only), drive the client
        # over https so the middleware's re-signed cookie is retained and resent across requests.
        https = operator and str(settings.public_base_url or "").startswith("https://")
        with TestClient(app, base_url="https://testserver" if https else "http://testserver") as c:
            if operator:
                c.cookies.set("session", operator_session_cookie(subject=operator_subject, username=operator_username))
            yield c

    return _make


@pytest.fixture
def client(make_client: Callable[..., Any]) -> Iterator[TestClient]:
    with make_client() as c:
        yield c


@pytest.fixture
def make_operator_client(make_client: Callable[..., Any]) -> Callable[..., Any]:
    """`make_client` with `operator=True` baked in: the app runs in the production app-owned-auth
    mode (SessionMiddleware + active router guards) and the client carries an authenticated operator
    session. Pass the same `Settings` overrides you would to `make_client`."""

    @contextmanager
    def _make(**settings_overrides: Any) -> Iterator[TestClient]:
        with make_client(operator=True, **settings_overrides) as c:
            yield c

    return _make
