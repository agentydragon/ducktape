"""Owner login settings and wiring shared by session services."""

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr, ValidationInfo

from util.oidc_login import LoginConfig, current_session_is_valid, install_login


def validate_https_url(value: str, info: ValidationInfo) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Expected an HTTPS URL without credentials, query or fragment")
    if info.field_name == "public_base_url" and url.path not in {"", "/"}:
        raise ValueError("Public base URL must be an origin")
    return value.rstrip("/")


def validate_secret(value: SecretStr) -> SecretStr:
    if len(value.get_secret_value()) < 32:
        raise ValueError("Secrets must contain at least 32 characters")
    return value


def require_owner_session(request: Request) -> None:
    """Require the signed owner cookie for provider-prefixed owner API routes."""
    if not current_session_is_valid(request, request.app.state.owner_login_config):
        raise HTTPException(status_code=401, detail="Not authenticated")


def install_owner_login(
    app: FastAPI,
    settings,
    *,
    cookie_name: str,
    public_paths: set[str],
    session_created_log: str,
    auth_handled_paths: set[str] | frozenset[str] = frozenset(),
) -> None:
    config = LoginConfig(
        issuer=settings.issuer.rstrip("/") + "/",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        session_secret=settings.session_secret,
        session_seconds=3600,
        public_base_url=settings.public_base_url,
        cookie_name=cookie_name,
        public_paths=public_paths,
        signed_out_text="You have signed out.",
        session_created_log=session_created_log,
        allowed_subject=settings.allowed_subject,
        auth_handled_paths=auth_handled_paths,
    )
    app.state.owner_login_config = config
    install_login(app, config)
