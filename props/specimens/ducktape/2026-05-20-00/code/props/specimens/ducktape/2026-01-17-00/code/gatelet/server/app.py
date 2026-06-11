"""FastAPI application for Gatelet server."""

import logging
from datetime import datetime

from fastapi import Cookie, Depends, FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import select

from gatelet.server.auth.handlers import AuthHandlerError
from gatelet.server.auth.webhook_auth import AuthError
from gatelet.server.database import get_db_session
from gatelet.server.endpoints import admin, challenge, homeassistant, webhook_receive, webhook_view
from gatelet.server.lifespan import BASE_DIR, lifespan
from gatelet.server.models import AdminSession

logger = logging.getLogger(__name__)


async def auth_error_handler(request: Request, exc: AuthHandlerError):
    """Handle all auth errors in HTML-friendly way."""
    return request.app.state.templates.TemplateResponse(
        "error.html",
        {"request": request, "status_code": status.HTTP_401_UNAUTHORIZED, "detail": "Authentication failed"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


async def webhook_auth_error(request: Request, exc: AuthError):
    """Handle webhook auth errors in API-friendly way."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Authentication failed"},
        headers=getattr(exc, "headers", {}),
    )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Route registration happens in lifespan startup, not here.
    This avoids import-time config reading.
    """
    application = FastAPI(
        title="Gatelet", description="LLM-friendly API for Home Assistant and webhooks", lifespan=lifespan
    )
    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    # Include routers (these don't read config at import time)
    application.include_router(webhook_receive.router)
    application.include_router(webhook_view.router)
    application.include_router(challenge.router)
    application.include_router(admin.router)
    application.include_router(homeassistant.router)

    # Register exception handlers
    application.add_exception_handler(AuthHandlerError, auth_error_handler)
    application.add_exception_handler(AuthError, webhook_auth_error)

    return application


app = create_app()


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, session: str | None = Cookie(None), csrf_protect: CsrfProtect = Depends()):
    """Root endpoint with service information and authentication options."""
    # Check if already authenticated via cookie
    if session:
        async with get_db_session(request) as db_session:
            stmt = select(AdminSession).where(AdminSession.session_token == session)
            admin_session = (await db_session.execute(stmt)).scalar_one_or_none()
            if admin_session and admin_session.expires_at > datetime.now():
                return RedirectResponse("/admin/", status_code=302)

    token, signed = csrf_protect.generate_csrf_tokens()
    response = request.app.state.templates.TemplateResponse(
        "public.html",
        {
            "request": request,
            "header": "Gatelet",
            "show_admin_login": True,
            "llm_instructions": "To access this service as an LLM, follow the instructions provided by your user.",
            "csrf_token": token,
        },
    )
    csrf_protect.set_csrf_cookie(signed, response)
    return response
