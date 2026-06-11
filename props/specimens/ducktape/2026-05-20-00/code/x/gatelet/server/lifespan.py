"""Application lifespan management for Gatelet server.

Handles startup and shutdown of application-scoped resources (database engine, templates, etc.).
"""

import logging
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Literal

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from x.gatelet.server.auth.dependencies import (
    Auth,
    get_admin_auth_with_context,
    get_key_path_auth_with_context,
    get_session_auth_with_context,
)
from x.gatelet.server.config import Settings, get_settings
from x.gatelet.server.database import get_db_session
from x.gatelet.server.endpoints import activitywatch, homeassistant, webhook_view
from x.gatelet.server.endpoints.homeassistant import fetch_states
from x.gatelet.server.endpoints.webhook_view import get_latest_payloads

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


def _format_minutes(td: timedelta) -> str:
    """Jinja2 filter: format a timedelta as minutes."""
    total_seconds = td.total_seconds() if isinstance(td, timedelta) else float(td)
    return f"{total_seconds / 60:.1f}m"


class _CsrfSettings(BaseModel):
    """CSRF config for fastapi-csrf-protect.

    See LoadConfig in fastapi_csrf_protect/load_config.py for all options.
    token_key is required when token_location is "body".
    """

    secret_key: str
    token_location: Literal["body", "header"]
    token_key: str | None = None  # Required if token_location is "body"


def _init_csrf_config(csrf_secret: str) -> None:
    """Initialize CSRF protection config.

    Called during app startup to configure CsrfProtect with settings.
    This defers config loading from import time to runtime.
    """
    CsrfProtect.load_config(
        lambda: _CsrfSettings(secret_key=csrf_secret, token_location="body", token_key="csrf_token")
    )


def _register_auth_routes(app: FastAPI, settings: Settings) -> None:
    """Register routes with authentication wrappers.

    Called during lifespan startup after settings are available.
    This defers route registration from import time to runtime.
    """

    def register_with_all_auth_methods(path: str, handler: Callable, *, register_admin: bool = True) -> None:
        """Register a handler with all available auth methods."""
        # Key in path auth
        if settings.auth.key_in_url.enabled:
            app.add_api_route(
                f"/k/{{key}}{path}",
                handler,
                methods=["GET"],
                response_class=HTMLResponse,
                dependencies=[Depends(get_key_path_auth_with_context)],
            )

        # Challenge-response session auth
        if settings.auth.challenge_response.enabled:
            app.add_api_route(
                f"/s/{{session_token}}{path}",
                handler,
                methods=["GET"],
                response_class=HTMLResponse,
                dependencies=[Depends(get_session_auth_with_context)],
            )

        if register_admin:
            app.add_api_route(
                f"/admin{path}",
                handler,
                methods=["GET"],
                response_class=HTMLResponse,
                dependencies=[Depends(get_admin_auth_with_context)],
            )

    # Handler for authenticated root — uses settings from the enclosing
    # _register_auth_routes scope rather than Depends(get_settings), which
    # would capture the wrong function reference when monkeypatched in tests.
    async def authenticated_root_handler(request: Request, auth: Auth):
        """Shared handler for authenticated root endpoint."""
        async with get_db_session(request) as db_session:
            recent = await get_latest_payloads(db_session, limit=5)
        ha_states = await fetch_states(settings)
        aw_summary = await activitywatch.fetch_recent_activity(settings.activitywatch)
        return request.app.state.templates.TemplateResponse(
            "index.html.j2",
            {
                "request": request,
                "header": "Gatelet",
                "auth": auth,
                "recent_payloads": recent,
                "ha_states": ha_states,
                "aw_activity": aw_summary,
            },
        )

    # Register routes
    register_with_all_auth_methods("/", authenticated_root_handler, register_admin=False)
    register_with_all_auth_methods("/webhooks/", webhook_view.list_all_payloads)
    register_with_all_auth_methods("/webhooks/{integration_name}", webhook_view.list_integration_payloads)
    register_with_all_auth_methods("/ha/", homeassistant.list_entities)
    register_with_all_auth_methods("/ha/{entity_id}", homeassistant.entity_details)

    logger.info("Auth-wrapped routes registered")


def _run_migrations(dsn: str) -> None:
    """Run alembic migrations to head."""
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", dsn.replace("+asyncpg", ""))
    logger.info("Running database migrations...")
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Database migrations complete")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan: startup and shutdown.

    - On startup: Initialize database engine, session factory, and templates
    - On shutdown: Dispose of database engine

    Args:
        app: FastAPI application instance

    Resources stored on app.state:
        - db_engine: AsyncEngine
        - db_session_factory: async_sessionmaker[AsyncSession]
        - templates: Jinja2Templates
    """
    # Startup
    logger.info("Starting Gatelet server...")
    settings = get_settings()

    # Run migrations (opt-in via env var for container deployments).
    # Tests manage their own schema via Base.metadata.create_all in conftest fixtures.
    if os.environ.get("GATELET_RUN_MIGRATIONS") == "1":
        _run_migrations(str(settings.database.dsn))

    # Initialize CSRF protection (deferred from import time)
    _init_csrf_config(settings.security.csrf_secret)
    logger.info("CSRF protection configured")

    # Register auth-wrapped routes (deferred from import time)
    _register_auth_routes(app, settings)

    # Create database engine
    engine = create_async_engine(str(settings.database.dsn), echo=False, future=True, pool_pre_ping=True)
    app.state.db_engine = engine
    logger.info(f"Database engine created for: {settings.database.dsn}")

    # Create session factory
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    app.state.db_session_factory = session_factory
    logger.info("Database session factory created")

    # Create templates instance
    app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")
    # Add Python builtins needed by templates
    app.state.templates.env.globals.update({"max": max, "min": min})
    # Add custom filters
    app.state.templates.env.filters["minutes"] = _format_minutes
    logger.info("Templates initialized")

    yield

    # Shutdown
    logger.info("Shutting down Gatelet server...")
    await engine.dispose()
    logger.info("Database engine disposed")
