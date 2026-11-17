"""Casino identity admission uses the shared login for HTTP and WebSockets."""

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.websockets import WebSocketDisconnect

from study_casino.app import create_app
from study_casino.config import Settings
from study_casino.store import SqlStore
from util.testing.asgi import bind_http_server
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

_SESSION_SECRET = "test-session-secret-at-least-32-characters"


@pytest.fixture
def settings(db_url: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=db_url,
        frontend_dist_dir=tmp_path / "absent",
        oidc_issuer="https://idp.test/",
        oidc_client_id="casino-test",
        oidc_client_secret="test-client-secret",
        session_secret=_SESSION_SECRET,
        rng_secret="test-rng-secret-at-least-32-characters",
        public_url="https://casino.test",
        admin_users={"fixture-user"},
    )


def test_login_keeps_existing_username_ownership_and_websocket_admission(settings: Settings) -> None:
    private, public = generate_rsa_keypair()
    store = SqlStore(settings.database_url)
    store.state_dump("fixture-user")
    with bind_http_server() as idp, bind_http_server() as server:
        idp_app = build_mock_oidc_app(
            issuer_url=idp.origin,
            private_key=private,
            public_key=public,
            subject="unrelated-immutable-subject",
            extra_id_token_claims={"preferred_username": "fixture-user"},
        )
        app = create_app(
            settings.model_copy(update={"oidc_issuer": idp.origin, "public_url": server.origin}), store=store
        )
        with idp.serve_sync(idp_app), server.serve_sync(app), httpx.Client(base_url=server.origin) as browser:
            browser.get("/auth/login", follow_redirects=True)
            assert browser.get("/me").json() == {"username": "fixture-user", "is_admin": True}
            assert browser.get("/state").status_code == 200
            assert store.list_known_users() == ["fixture-user"]
            with TestClient(app, base_url=server.origin) as client:
                client.cookies.update(browser.cookies)
                with client.websocket_connect(f"{server.origin.replace('http:', 'ws:')}/ws") as websocket:
                    assert websocket.receive_json() == {"type": "state_changed"}
            assert browser.post("/auth/logout", headers={"Origin": server.origin}).status_code == 303
            assert browser.get("/me").status_code == 401


@pytest.mark.parametrize("invalid", ["missing", "expired", "other-issuer", "forged"])
def test_rejected_sessions_cannot_read_state_or_connect_websockets(settings: Settings, invalid: str) -> None:
    payload = {
        "issuer": "https://other.test/" if invalid == "other-issuer" else settings.oidc_issuer,
        "subject": "unrelated-immutable-subject",
        "username": "fixture-user",
        "expires_at": 1 if invalid == "expired" else time.time() + 600,
    }
    encoded = base64.b64encode(json.dumps({"user": payload}).encode())
    secret = "forged-secret" if invalid == "forged" else _SESSION_SECRET
    cookie = TimestampSigner(secret).sign(encoded).decode()
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        if invalid != "missing":
            client.cookies.set("__Host-casino_session", cookie)
        assert client.get("/state").status_code == 401
        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect("wss://casino.test/ws"):
            pass
        assert closed.value.code == 4001
