"""Fake owner settings and a signed owner session for the archive web tests."""

import base64
import json
import time

from itsdangerous import TimestampSigner

from ai_session_archive.web_settings import WebSettings


def fake_web_settings(*, public_base_url: str = "http://testserver") -> WebSettings:
    return WebSettings(
        database_url="sqlite://",
        public_base_url=public_base_url,
        oidc_issuer="https://idp.example.invalid",
        oidc_client_id="fake-client",
        oidc_client_secret="fake-client-secret",
        oidc_session_secret="fake-session-secret",
        oidc_allowed_subject="fake-owner",
    )


def owner_session_cookie(settings: WebSettings) -> str:
    session = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": settings.oidc_allowed_subject,
            "username": "fake-user",
            "expires_at": time.time() + 600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(settings.oidc_session_secret.get_secret_value()).sign(payload).decode()
