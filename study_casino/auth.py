"""Shared Authentik login with Casino-owned HTTP and WebSocket admission."""

from collections.abc import Callable

from fastapi import HTTPException, Request
from pydantic import SecretStr

from study_casino.config import OidcConfig
from util.oidc_login import LoginConfig, current_session


def login_config(oidc: OidcConfig) -> LoginConfig:
    return LoginConfig(
        issuer=oidc.issuer,
        client_id=oidc.client_id,
        client_secret=SecretStr(oidc.client_secret),
        session_secret=SecretStr(oidc.session_secret.decode()),
        session_seconds=30 * 24 * 3600,
        public_base_url=oidc.public_url.rstrip("/"),
        cookie_name="casino_session",
        public_paths=frozenset(),
        signed_out_text="You are signed out of Study Casino.",
        session_created_log="Study Casino OIDC login",
        allowed_subject=None,
    )


def make_current_user_dep(config: LoginConfig | None) -> Callable[[Request], str]:
    def current_user(request: Request) -> str:
        if config is None:
            return "default"
        session = current_session(request, config)
        if session is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        # Existing rows and admin policy are keyed by username, never the OIDC subject.
        return session.username

    return current_user
