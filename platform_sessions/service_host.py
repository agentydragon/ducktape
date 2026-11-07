"""Service assembly for configured session integrations."""

from typing import Any

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from platform_sessions.auth import BearerConfig
from platform_sessions.host import create_app as create_host, create_shell
from platform_sessions.owner_web import install_owner_login, require_owner_session, validate_https_url, validate_secret
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.remote_management import create_remote_management_router
from platform_sessions.proxy import install_proxy
from platform_sessions.proxy_support import HeaderConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_SESSIONS_", frozen=True)
    issuer: str
    public_base_url: str
    client_id: str
    client_secret: SecretStr
    session_secret: SecretStr
    allowed_subject: str
    port: int = Field(default=8080, ge=1, le=65535)
    integration_config: dict[str, Any]
    bearers: dict[str, BearerConfig] = Field(default_factory=dict, repr=False)
    proxy_headers: dict[str, HeaderConfig] = Field(default_factory=dict)

    _validate_urls = field_validator("issuer", "public_base_url")(validate_https_url)
    _validate_secrets = field_validator("client_secret", "session_secret")(validate_secret)


def create_app(settings, runtimes, proxy_instances, auth, *, transport=None, start=None, stop=None, ready=None):
    app = create_host(
        runtimes,
        settings.public_base_url,
        require_owner=require_owner_session,
        auth=auth,
        transport=transport,
        start=start,
        stop=stop,
    )
    return _install_gateway_routes(app, settings, proxy_instances, auth, ready=ready)


def create_remote_app(settings, clients: dict[str, ProviderControlClient], proxy_instances, auth):
    """Build a gateway backed exclusively by remote provider control services."""
    app = create_shell()
    app.include_router(
        create_remote_management_router(
            clients, settings.public_base_url, require_owner=require_owner_session, auth=auth
        )
    )
    return _install_gateway_routes(app, settings, proxy_instances, auth)


def _install_gateway_routes(app, settings, proxy_instances, auth, *, ready=None):
    install_proxy(app, proxy_instances, auth)

    class PublicPaths(set):
        def __contains__(self, path):
            return (
                super().__contains__(path)
                or path in {"/proxy", "/api/integrations"}
                or path.startswith(("/proxy/", "/api/integrations/"))
            )

    install_owner_login(
        app,
        settings,
        cookie_name="platform-sessions",
        public_paths=PublicPaths({"/healthz"}),
        session_created_log="Platform session owner sign-in succeeded",
    )

    @app.middleware("http")
    async def disable_caching(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def healthz():
        if ready is not None and not ready():
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    return app


def run(app, port):
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, proxy_headers=False)
