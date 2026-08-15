"""Shared fixtures for haku/console tests: a real Postgres (testcontainer) and a TestClient
over the console app wired to it.

Postgres is required by the console (approval ledger + operator OAuth store), so every test that
builds the app runs against a fresh per-test database. `db_url` is a pristine empty database (used by
the migration tests, which drive alembic themselves); `migrated_db_url` is the same database upgraded
to head (used by everything else, including `make_client`).

This file is expensive to *import*, not just to use: `create_app` drags in the whole composition
root, which pytest pays at collection for every test under `haku/console/` whose target depends on
`//haku/console:conftest` — fixtures nobody requested included. So a subpackage of leaf tests that
uses none of these fixtures should leave the dep off (see `tools/BUILD.bazel`).
"""

from __future__ import annotations

import base64
import json
import re
import textwrap
import time
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import itsdangerous
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

from haku.console.app import create_app
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig, Settings, WebPushConfig
from haku.console.database_migrate import apply_migrations
from haku.console.operator_auth import OPERATOR_SESSION_MAX_AGE_SECONDS, SESSION_USER_KEY
from haku.console.operator_identity import OperatorIdentityTrust, ResolvedOperatorIdentity, VerifiedExternalIdentity
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import OperatorActor
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import start_postgres_container

# A default static agent so `create_app`'s require-a-/mcp-credential invariant is satisfied without
# every test spelling one out — the real deploy always has the `haku` agent. Tests that exercise
# agent auth pass their own `config_file` naming the agents (and bearer) they need.
_DEFAULT_AGENT_TOKEN_ENV = "HAKU_CONSOLE_DEFAULT_AGENT_TOKEN"
_DEFAULT_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_DEFAULT_AGENT_OPERATOR"
_DEFAULT_STATIC_AGENTS = [
    {
        "agent_id": "00000000-0000-4000-8000-000000000001",
        "display_name": "Console Test Agent",
        "token_env_var": _DEFAULT_AGENT_TOKEN_ENV,
        "operator_subject_env": _DEFAULT_AGENT_OPERATOR_ENV,
        "auto_approval_policy": "no_auto_approval",
    }
]

# App-owned operator auth for tests. A dummy `operator_oidc` (no live IdP needed) activates
# SessionMiddleware + the router guards exactly as production does; tests inject the operator session
# cookie directly rather than walking the full Authentik login (that end-to-end path is covered by
# test_operator_auth). The retired forward-auth `x-authentik-*` headers are no longer trusted, so a
# session is the only way to present operator identity.
_TEST_SESSION_SECRET = "test-operator-session-secret"
_TEST_PUBLIC_BASE_URL = "https://haku.test"
TEST_OPERATOR_OIDC = OperatorOidcConfig(
    issuer="https://auth.test/application/o/haku-console/",
    client_id="console",
    client_secret=SecretStr("client-secret"),
    session_secret=SecretStr(_TEST_SESSION_SECRET),
)
TEST_OPERATOR_IDENTITY = OperatorIdentityConfig(trust_domain="auth.test/authentik-user-id/v1")

# A throwaway P-256 VAPID key, generated for these tests only. A VAPID keypair is an
# application's identity to a browser push service, not a secret shared with anyone, so a
# checked-in test key discloses nothing. Shared here because both the delivery tests and the
# subscription-route tests need a console that has push configured.
TEST_VAPID_PEM = textwrap.dedent("""\
    -----BEGIN PRIVATE KEY-----
    MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg24QW6LTvVznZN4jU
    BILIbhfwLSMUn0cFYt1jFuT6upehRANCAAQo3F6m6cvV2kLzc0dUok0RtwqSjmpe
    xyCRbeJXwDklyWC0XHraZH5zUtaFz3p5PIiJiw61hBbrvg2YoQzSDroL
    -----END PRIVATE KEY-----
    """)
TEST_WEB_PUSH = WebPushConfig(private_key_pem=SecretStr(TEST_VAPID_PEM), subject="mailto:operator@example.com")


def operator_session_cookie(
    *,
    operator_id: str,
    identity_id: str,
    username: str,
    expires_at: int | None = None,
    browser_session_id: str = "test-browser-session-id",
) -> str:
    """A Starlette SessionMiddleware `session` cookie for a logged-in operator, mirroring its own
    sign format (`TimestampSigner` over base64-JSON) so a TestClient can present operator identity
    without walking the live OIDC login."""
    operator_session: dict[str, object] = {
        "operator_id": operator_id,
        "identity_id": identity_id,
        "username": username,
        "browser_session_id": browser_session_id,
        "expires_at": expires_at if expires_at is not None else int(time.time()) + OPERATOR_SESSION_MAX_AGE_SECONDS,
    }
    session: dict[str, object] = {SESSION_USER_KEY: operator_session}
    data = base64.b64encode(json.dumps(session).encode())
    return itsdangerous.TimestampSigner(_TEST_SESSION_SECRET).sign(data).decode()


def write_config(path: Path, config: dict[str, Any]) -> Path:
    """Dump a console config dict to `path` as YAML (the deploy-time config-file format)."""
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def console_settings(migrated_db_url: str, **overrides: Any) -> Settings:
    """The console `Settings` tests need — required `haku_ui_url`/`database_url` defaulted, per-test
    `overrides` spread last. Shared by `make_client` and by the tests that serve `create_app` over a
    real socket (so they can't go through `make_client`'s `TestClient`)."""
    return Settings(
        **{
            "haku_ui_url": "https://haku-ui.test",
            "database_url": SecretStr(migrated_db_url),
            "public_base_url": "https://haku.test",
            "operator_oidc": TEST_OPERATOR_OIDC,
            "operator_identity": TEST_OPERATOR_IDENTITY,
            **overrides,
        }
    )


def console_sessions(db_url: str) -> async_sessionmaker[AsyncSession]:
    """The shared async sessionmaker tests inject into stores, mirroring create_app's one-engine wiring
    (production builds a single async engine/sessionmaker and passes it to every store)."""
    async_url = make_url(db_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    return async_sessionmaker(create_async_engine(async_url, pool_pre_ping=True), expire_on_commit=False)


def operator_identity_store(db_url: str) -> PostgresOperatorIdentityStore:
    """The `PostgresOperatorIdentityStore` tests need, trusting the app-owned test OIDC issuer."""
    return PostgresOperatorIdentityStore(
        console_sessions(db_url),
        OperatorIdentityTrust(
            trust_domain=TEST_OPERATOR_IDENTITY.trust_domain, trusted_issuers=frozenset({TEST_OPERATOR_OIDC.issuer})
        ),
    )


async def _resolve_operator_identity(app: Any, external_user_key: str) -> ResolvedOperatorIdentity:
    """Create the same issuer-scoped browser identity that the OIDC callback would persist."""
    return cast(
        ResolvedOperatorIdentity,
        await app.state.operator_identity_store.resolve_verified_identity(
            VerifiedExternalIdentity(issuer=app.state.settings.operator_oidc.issuer, subject=external_user_key)
        ),
    )


async def operator_id(sessions: async_sessionmaker[AsyncSession], external_user_key: str) -> UUID:
    """Resolve a controller-fed external user key using the caller's async sessionmaker."""
    store = PostgresOperatorIdentityStore(
        sessions,
        OperatorIdentityTrust(
            trust_domain=TEST_OPERATOR_IDENTITY.trust_domain, trusted_issuers=frozenset({TEST_OPERATOR_OIDC.issuer})
        ),
    )
    return await store.resolve_configured_external_user_key(external_user_key)


async def resolve_operator_identity(
    sessions: async_sessionmaker[AsyncSession], *, issuer: str, subject: str
) -> ResolvedOperatorIdentity:
    """Resolve a test OIDC identity using the caller's async sessionmaker."""
    store = PostgresOperatorIdentityStore(
        sessions,
        OperatorIdentityTrust(trust_domain=TEST_OPERATOR_IDENTITY.trust_domain, trusted_issuers=frozenset({issuer})),
    )
    return await store.resolve_verified_identity(VerifiedExternalIdentity(issuer=issuer, subject=subject))


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    """Postgres **with pgvector**, overriding the shared stock-image fixture.

    Migration 0037 creates `vector` columns, so every test that migrates to head needs an image
    that has the extension — the same capability production gets from the CNPG image.
    """
    container = start_postgres_container(PGVECTOR_PG18)
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
    # `vector` here rather than in a migration: pgvector is untrusted, so only a superuser can
    # install it, which is why the deployed database gets it from CNPG's `Database` CR.
    db_url = create_database_sync(postgres_admin_url, db_name, extensions=("vector",))

    yield make_url(db_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

    force_drop_database_sync(postgres_admin_url, db_name)


@pytest.fixture
def migrated_db_url(db_url: str) -> str:
    """The per-test database upgraded to head — what the app expects at runtime (migrations run as an
    explicit startup step, not inside a store constructor)."""
    apply_migrations(db_url)
    return db_url


@pytest.fixture
async def migrated_engine(migrated_db_url: str) -> AsyncGenerator[AsyncEngine]:
    """A shared async engine for tests that need direct database access.

    The fixture owns disposal so tests can share the same pool without leaking one engine per
    helper call.
    """
    engine = create_async_engine(migrated_db_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def migrated_sessions(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker bound to the shared migrated test engine."""
    return async_sessionmaker(migrated_engine, expire_on_commit=False)


@pytest.fixture
def migrated_identity_store(migrated_sessions: async_sessionmaker[AsyncSession]) -> PostgresOperatorIdentityStore:
    """Operator identity store bound to the shared migrated test engine."""
    return PostgresOperatorIdentityStore(
        migrated_sessions,
        OperatorIdentityTrust(
            trust_domain=TEST_OPERATOR_IDENTITY.trust_domain, trusted_issuers=frozenset({TEST_OPERATOR_OIDC.issuer})
        ),
    )


@pytest.fixture
def make_client(migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Factory: a TestClient over the console app on a fresh migrated database, with optional
    ``Settings`` overrides (e.g. ``config_file=...``, ``mcp_oauth=...``)."""
    monkeypatch.setenv(_DEFAULT_AGENT_TOKEN_ENV, "default-agent-token")
    monkeypatch.setenv(_DEFAULT_AGENT_OPERATOR_ENV, "default-op")
    default_config = write_config(
        tmp_path / "console_default.yaml",
        {
            "static_agents": _DEFAULT_STATIC_AGENTS,
            "auto_approval_policies": [{"id": "no_auto_approval", "type": "never"}],
        },
    )

    @contextmanager
    def _make(
        *,
        gmail_client: Any | None = None,
        in_process_servers: Any | None = None,
        config_file: Path | None = None,
        operator: bool = False,
        operator_external_user_key: str = "operator-sub",
        operator_username: str = "operator@example.com",
        operator_session_expires_at: int | None = None,
        **settings_overrides: Any,
    ) -> Iterator[TestClient]:
        # Every app uses production-shaped OIDC settings. `operator=True` presents an authenticated
        # signed session; false leaves the client anonymous for auth-boundary/static-agent tests.
        # config_file defaults to the config naming the default static agent (so /mcp has a
        # credential); a test overrides it by passing its own. haku_ui_url + database_url come from
        # console_settings.
        settings = console_settings(
            migrated_db_url,
            config_file=config_file if config_file is not None else default_config,
            **settings_overrides,
        )
        app = create_app(settings, gmail_client=gmail_client, in_process_servers=in_process_servers)
        # When the session cookie is Secure (https public_base_url → https_only), drive the client
        # over https so the middleware's re-signed cookie is retained and resent across requests.
        https = settings.public_base_url.startswith("https://")
        with TestClient(app, base_url="https://testserver" if https else "http://testserver") as c:
            if operator:
                assert c.portal is not None
                operator_identity = c.portal.call(_resolve_operator_identity, app, operator_external_user_key)
                app.state.test_operator_actor = OperatorActor(operator_id=operator_identity.operator_id)
                c.headers["Origin"] = settings.public_base_url.rstrip("/")
                c.cookies.set(
                    "session",
                    operator_session_cookie(
                        operator_id=str(operator_identity.operator_id),
                        identity_id=str(operator_identity.identity_id),
                        username=operator_username,
                        expires_at=operator_session_expires_at,
                    ),
                )
            yield c

    return _make


@pytest.fixture
def client(make_operator_client: Callable[..., Any]) -> Iterator[TestClient]:
    with make_operator_client() as c:
        yield c


@pytest.fixture
def make_operator_client(make_client: Callable[..., Any]) -> Callable[..., Any]:
    """`make_client` with `operator=True` baked in: the app runs in the production app-owned-auth
    mode (SessionMiddleware + active router guards) and the client carries an authenticated operator
    session. A stable public-origin default is supplied; pass the same `Settings` overrides
    you would to `make_client` when a test needs different values."""

    @contextmanager
    def _make(**settings_overrides: Any) -> Iterator[TestClient]:
        settings_overrides.setdefault("public_base_url", _TEST_PUBLIC_BASE_URL)
        with make_client(operator=True, **settings_overrides) as c:
            yield c

    return _make
