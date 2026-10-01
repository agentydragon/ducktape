"""Authenticated Plaid card-spend API and browser settings page."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from typing import Annotated, cast

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError

from finance.plaid.spend.models import CardConfiguration, SettingsState, SpendView
from finance.plaid.spend.service import SpendService, UnknownCreditAccountsError
from finance.plaid.spend.settings import SpendSettings
from mcp_infra.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)
from util.oidc_login import LoginConfig, LoginSession, install_login

_ASSETS = resources.files("finance.plaid.spend")
_SETTINGS_HTML = _ASSETS.joinpath("settings.html").read_text("utf-8")
_SETTINGS_JS = _ASSETS.joinpath("settings.js").read_text("utf-8")


async def _require_api_principal(request: Request) -> VerifiedOidcPrincipal:
    resolver: AuthentikOidcPrincipalResolver = request.app.state.principal_resolver
    authorization = request.headers.get("authorization", "").split()
    if len(authorization) != 2 or authorization[0].casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await resolver.resolve({"access_token": authorization[1], "token_type": "Bearer"})
    except InvalidOidcPrincipalError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is invalid",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except OidcPrincipalVerificationUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="OIDC verification unavailable"
        ) from error


def _browser_subject(request: Request) -> str:
    try:
        session = LoginSession.model_validate(request.session.get("user"))
    except ValidationError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated") from error
    return session.subject


async def _spend_service(request: Request) -> SpendService:
    return cast(SpendService, request.app.state.spend_service)


ApiPrincipal = Annotated[VerifiedOidcPrincipal, Depends(_require_api_principal)]
BrowserSubject = Annotated[str, Depends(_browser_subject)]
SpendDatabase = Annotated[SpendService, Depends(_spend_service)]


def _login_config(settings: SpendSettings) -> LoginConfig:
    return LoginConfig(
        issuer=settings.browser_oidc_issuer,
        client_id=settings.browser_oidc_client_id,
        client_secret=settings.browser_oidc_client_secret,
        session_secret=settings.browser_oidc_session_secret,
        session_seconds=settings.browser_oidc_session_seconds,
        public_base_url=settings.public_base_url,
        cookie_name="plaid-spend-session",
        public_paths={"/healthz", "/api/v1/view", "/api/v1/events", "/api/v1/config"},
        signed_out_text="You are signed out of Plaid Spend.",
        session_created_log="Authentik session created for Plaid Spend",
        allowed_subject=None,
    )


def create_app(settings: SpendSettings, *, service: SpendService | None = None) -> FastAPI:
    runtime_service = service or SpendService(settings.database_url)
    resolver = AuthentikOidcPrincipalResolver(
        expected_issuer=settings.api_oidc_issuer,
        discovered_issuer=settings.api_oidc_discovered_issuer,
        jwks_uri=settings.api_oidc_jwks_uri,
        signing_algorithms=settings.api_signing_algorithms,
        client_id=settings.api_oidc_client_id,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime_service.start()
        try:
            yield
        finally:
            await runtime_service.close()

    app = FastAPI(title="Plaid Spend", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.spend_service = runtime_service
    app.state.principal_resolver = resolver
    app.state.public_base_url = settings.public_base_url
    install_login(app, _login_config(settings))

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> str:
        return _SETTINGS_HTML

    @app.get("/settings.js", include_in_schema=False)
    async def settings_script() -> Response:
        return Response(_SETTINGS_JS, media_type="text/javascript")

    @app.get("/settings-data/state", response_model=SettingsState)
    async def settings_state(subject: BrowserSubject, database: SpendDatabase) -> SettingsState:
        accounts, configuration = await database.settings_state(subject)
        return SettingsState(accounts=list(accounts), config=configuration)

    @app.put("/settings-data/config", response_model=CardConfiguration)
    async def update_settings_config(
        request: Request, configuration: CardConfiguration, subject: BrowserSubject, database: SpendDatabase
    ) -> CardConfiguration:
        _require_same_origin(request, request.app.state.public_base_url)
        try:
            await database.replace_configuration(subject, configuration)
        except UnknownCreditAccountsError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"unknown_or_inactive_account_ids": list(error.account_ids)},
            ) from error
        return configuration

    @app.get("/api/v1/view", response_model=SpendView)
    async def get_view(principal: ApiPrincipal, database: SpendDatabase) -> SpendView:
        return await database.read_view(principal.subject)

    @app.get("/api/v1/config", response_model=CardConfiguration)
    async def get_config(principal: ApiPrincipal, database: SpendDatabase) -> CardConfiguration:
        return await database.get_configuration(principal.subject)

    @app.put("/api/v1/config", response_model=CardConfiguration)
    async def put_config(
        configuration: CardConfiguration, principal: ApiPrincipal, database: SpendDatabase
    ) -> CardConfiguration:
        try:
            await database.replace_configuration(principal.subject, configuration)
        except UnknownCreditAccountsError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"unknown_or_inactive_account_ids": list(error.account_ids)},
            ) from error
        return configuration

    @app.get("/api/v1/events")
    async def events(request: Request, principal: ApiPrincipal, database: SpendDatabase) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(request, database, principal.subject),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return app


async def _event_stream(request: Request, service: SpendService, subject: str) -> AsyncIterator[str]:
    queue = service.subscribe()
    initial_sent = False
    try:
        while not await request.is_disconnected():
            if initial_sent or not service.listening.is_set():
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue

            if not service.listening.is_set():
                continue
            revision = service.revision
            view = await service.read_view(subject)
            if service.listening.is_set() and service.revision == revision:
                initial_sent = True
                yield _sse_view(view)
    finally:
        service.unsubscribe(queue)


def _sse_view(view: SpendView) -> str:
    data = json.dumps(view.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"event: view\ndata: {data}\n\n"


def _require_same_origin(request: Request, public_base_url: str) -> None:
    if request.headers.get("origin") != public_base_url:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = SpendSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
