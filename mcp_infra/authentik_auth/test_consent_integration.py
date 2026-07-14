"""Hermetic integration coverage for the Authentik-backed MCP consent boundary."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest_bazel
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.routing import Mount

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig, build_authentik_auth
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

_CLIENT_CALLBACK = "http://127.0.0.1:19876/callback"
_SCOPES = "openid email profile offline_access"


def _hidden_input(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]+name="{name}"[^>]+value="([^"]+)"', page)
    if match is None:
        raise AssertionError(f"OAuth consent page has no {name!r} input")
    return match.group(1)


def _authorization_params(*, client_id: str, state: str, code_verifier: str) -> dict[str, str]:
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _CLIENT_CALLBACK,
        "scope": _SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }


async def _complete_authorization(
    browser: httpx.AsyncClient, *, authorization_endpoint: str, token_endpoint: str, client_id: str, client_secret: str
) -> None:
    code_verifier = secrets.token_urlsafe(32)
    started = await browser.get(
        authorization_endpoint,
        params=_authorization_params(client_id=client_id, state="first", code_verifier=code_verifier),
    )
    assert started.status_code == 302

    consent = await browser.get(started.headers["location"], headers={"Sec-Fetch-Site": "same-origin"})
    assert consent.status_code == 200
    assert "Application Access Request" in consent.text

    approved = await browser.post(
        str(consent.url),
        data={
            "txn_id": _hidden_input(consent.text, "txn_id"),
            "csrf_token": _hidden_input(consent.text, "csrf_token"),
            "action": "approve",
        },
    )
    assert approved.status_code == 302

    upstream_callback = await browser.get(approved.headers["location"])
    assert upstream_callback.status_code == 302
    client_callback = await browser.get(upstream_callback.headers["location"])
    assert client_callback.status_code == 302
    callback_url = urlparse(client_callback.headers["location"])
    assert f"{callback_url.scheme}://{callback_url.netloc}{callback_url.path}" == _CLIENT_CALLBACK
    callback_params = parse_qs(callback_url.query)
    assert callback_params["state"] == ["first"]

    exchanged = await browser.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": callback_params["code"][0],
            "redirect_uri": _CLIENT_CALLBACK,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["access_token"]


async def test_build_authentik_auth_requires_consent_for_every_authorization() -> None:
    """The same browser and DCR client must not silently reuse an earlier approval."""
    private_key, public_key = generate_rsa_keypair()
    oidc_port = pick_free_port()
    mcp_port = pick_free_port()
    oidc_url = f"http://127.0.0.1:{oidc_port}"
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    idp = build_mock_oidc_app(issuer_url=oidc_url, private_key=private_key, public_key=public_key)

    async with serve_app(idp, port=oidc_port):
        auth = build_authentik_auth(
            AuthentikAuthConfig(
                oidc_issuer=oidc_url,
                oidc_client_id="mcp-test",
                oidc_client_secret="mcp-test-secret",
                public_base_url=mcp_url,
            ),
            client_storage=MemoryStore(),
        )
        mcp_app = FastMCP("Consent Test", auth=auth).http_app(path="/")
        app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
        async with serve_app(app, port=mcp_port), httpx.AsyncClient(follow_redirects=False) as browser:
            metadata_response = await browser.get(f"{mcp_url}/.well-known/oauth-authorization-server")
            assert metadata_response.status_code == 200, metadata_response.text
            metadata = metadata_response.json()
            registered_response = await browser.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": "Repeated Consent Test",
                    "redirect_uris": [_CLIENT_CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "client_secret_post",
                    "scope": _SCOPES,
                },
            )
            assert registered_response.status_code == 201, registered_response.text
            registered = registered_response.json()

            await _complete_authorization(
                browser,
                authorization_endpoint=metadata["authorization_endpoint"],
                token_endpoint=metadata["token_endpoint"],
                client_id=registered["client_id"],
                client_secret=registered["client_secret"],
            )

            second_verifier = secrets.token_urlsafe(32)
            second = await browser.get(
                metadata["authorization_endpoint"],
                params=_authorization_params(
                    client_id=registered["client_id"], state="second", code_verifier=second_verifier
                ),
            )
            assert second.status_code == 302
            # `"remember"` would silently redirect here: this browser retained its first approval,
            # and the safe-navigation header makes the shortcut eligible. `True` must render HTML.
            second_consent = await browser.get(second.headers["location"], headers={"Sec-Fetch-Site": "same-origin"})
            assert second_consent.status_code == 200
            assert "Application Access Request" in second_consent.text
            assert _hidden_input(second_consent.text, "txn_id")


if __name__ == "__main__":
    pytest_bazel.main()
