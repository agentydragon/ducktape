"""Authentik-authenticated Plaid card-spend web UI and desktop API."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, cast

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from finance.plaid.spend.models import SpendConfigurationView, SpendView, load_configuration
from finance.plaid.spend.service import SpendService
from finance.plaid.spend.settings import SpendSettings
from mcp_infra.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)
from util.oidc_login import LoginConfig, install_login

_UI_DIR = Path(__file__).resolve().parent / "ui" / "dist"


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


async def _spend_service(request: Request) -> SpendService:
    return cast(SpendService, request.app.state.spend_service)


ApiPrincipal = Annotated[VerifiedOidcPrincipal, Depends(_require_api_principal)]
SpendReader = Annotated[SpendService, Depends(_spend_service)]


def _web_login_config(settings: SpendSettings) -> LoginConfig:
    return LoginConfig(
        issuer=settings.web_oidc_issuer,
        client_id=settings.web_oidc_client_id.get_secret_value(),
        client_secret=settings.web_oidc_client_secret,
        session_secret=settings.web_oidc_session_secret,
        session_seconds=settings.web_oidc_session_seconds,
        public_base_url=settings.web_oidc_public_base_url,
        cookie_name="plaid-spend-session",
        public_paths={"/healthz", "/api/v1/view", "/api/v1/events"},
        signed_out_text="You are signed out of Plaid Spend.",
        session_created_log="Authentik session created for Plaid Spend",
        allowed_subject=None,
    )


def create_app(settings: SpendSettings, *, service: SpendService, include_ui: bool = True) -> FastAPI:
    resolver = AuthentikOidcPrincipalResolver(
        expected_issuer=settings.api_oidc_issuer,
        discovered_issuer=settings.api_oidc_discovered_issuer,
        jwks_uri=settings.api_oidc_jwks_uri,
        signing_algorithms=settings.api_signing_algorithms,
        client_id=settings.api_oidc_client_id,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="Plaid Spend", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.spend_service = service
    app.state.principal_resolver = resolver
    install_login(app, _web_login_config(settings))

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    if include_ui:

        @app.get("/", include_in_schema=False)
        async def root() -> FileResponse:
            return FileResponse(_UI_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=_UI_DIR), name="spend-ui")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/v1/view", response_model=SpendView)
    async def get_view(_principal: ApiPrincipal, reader: SpendReader) -> SpendView:
        return await reader.read_view()

    @app.get("/api/v1/web/view", response_model=SpendView)
    async def get_web_view(reader: SpendReader) -> SpendView:
        return await reader.read_view()

    @app.get("/api/v1/web/configuration", response_model=SpendConfigurationView)
    async def get_web_configuration(reader: SpendReader) -> SpendConfigurationView:
        return reader.read_configuration()

    @app.get("/api/v1/events")
    async def events(request: Request, _principal: ApiPrincipal, reader: SpendReader) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(request, reader),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/web/events")
    async def web_events(request: Request, reader: SpendReader) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(request, reader),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return app


async def _event_stream(request: Request, service: SpendService) -> AsyncGenerator[str]:
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
            view = await service.read_view()
            if service.listening.is_set() and service.revision == revision:
                initial_sent = True
                yield _sse_view(view)
    finally:
        service.unsubscribe(queue)


def _sse_view(view: SpendView) -> str:
    data = json.dumps(view.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"event: view\ndata: {data}\n\n"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = SpendSettings()
    configuration = load_configuration(settings.config_path)
    service = SpendService(settings.database_url, configuration, dashboard_url=settings.web_oidc_public_base_url)
    uvicorn.run(create_app(settings, service=service), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
