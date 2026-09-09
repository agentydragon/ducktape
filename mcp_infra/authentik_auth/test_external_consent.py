"""Probe the pinned public OAuth handoff seam, not a production consent implementation.

The test holds the upstream URL in a separate test ledger, representing the URL that an
authenticated consent BFF would release after its own browser/grant checks. FastMCP still
handles DCR, callback correlation, PKCE and tokens. No private FastMCP state is inspected.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest_bazel
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from mcp_infra.authentik_auth.fastmcp_proxy import DownstreamClientIdentityOIDCProxy
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

CALLBACK = "https://client.example.test/callback"
CONSENT = "https://integration.example.test/connections/consent/"
SCOPES = "openid email profile offline_access"


@dataclass(frozen=True)
class Pending:
    client_id: str
    redirect_uri: str
    code_challenge: str
    upstream_url: str


class HandoffProbe(DownstreamClientIdentityOIDCProxy):
    def __init__(self, *, oidc_url: str, base_url: str, storage: MemoryStore, pending: dict[str, Pending]) -> None:
        super().__init__(
            config_url=f"{oidc_url}/.well-known/openid-configuration",
            client_id="probe-upstream-client",
            client_secret="hermetic-probe-secret",
            base_url=base_url,
            client_storage=storage,
            require_authorization_consent="external",
            enable_cimd=False,
        )
        self.pending = pending
        self.update_default_scopes(SCOPES.split())

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        upstream_url = await super().authorize(client, params)
        assert client.client_id is not None
        interaction = secrets.token_urlsafe(32)
        self.pending[interaction] = Pending(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            code_challenge=params.code_challenge,
            upstream_url=upstream_url,
        )
        return f"{CONSENT}{interaction}"


async def test_public_handoff_preserves_validation_pkce_and_state_across_proxy_replacement() -> None:
    private_key, public_key = generate_rsa_keypair()
    oidc_port, service_port = pick_free_port(), pick_free_port()
    oidc_url = f"http://127.0.0.1:{oidc_port}"
    service_url = f"http://127.0.0.1:{service_port}"
    storage = MemoryStore()
    pending: dict[str, Pending] = {}
    idp = build_mock_oidc_app(issuer_url=oidc_url, private_key=private_key, public_key=public_key)

    async with serve_app(idp, port=oidc_port), httpx.AsyncClient(follow_redirects=False) as browser:
        proxy = HandoffProbe(oidc_url=oidc_url, base_url=service_url, storage=storage, pending=pending)
        app = FastMCP("External consent probe", auth=proxy).http_app(path="/mcp")
        async with serve_app(app, port=service_port):
            metadata = (await browser.get(f"{service_url}/.well-known/oauth-authorization-server")).json()
            registration = await browser.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": "Untrusted client presentation",
                    "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": SCOPES,
                },
            )
            assert registration.status_code == 201, registration.text
            client_id = registration.json()["client_id"]
            assert not pending  # Registration is machine-to-machine; it does not ask for consent.

            verifier = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "scope": SCOPES,
                "state": "first-tab",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": f"{service_url}/mcp",
            }
            invalid = await browser.get(
                metadata["authorization_endpoint"], params=params | {"redirect_uri": "https://other.example/callback"}
            )
            assert invalid.status_code == 400
            assert not pending  # Framework redirect validation precedes our public hook.

            invalid_resource = await browser.get(
                metadata["authorization_endpoint"], params=params | {"resource": "https://other.example/mcp"}
            )
            assert invalid_resource.status_code == 302
            # FastMCP raises invalid_target, absent from the pinned MCP SDK's error enum;
            # its handler therefore renders server_error. Admission still refuses the request.
            assert parse_qs(urlsplit(invalid_resource.headers["location"]).query)["error"] == ["server_error"]
            assert not pending  # super().authorize owns resource validation.

            first = await browser.get(metadata["authorization_endpoint"], params=params)
            second = await browser.get(metadata["authorization_endpoint"], params=params | {"state": "second-tab"})
            assert first.status_code == second.status_code == 302
            assert first.headers["location"].startswith(CONSENT)
            assert second.headers["location"].startswith(CONSENT)
            assert first.headers["location"] != second.headers["location"]
            held = pending[first.headers["location"].removeprefix(CONSENT)]
            assert (held.client_id, held.redirect_uri, held.code_challenge) == (client_id, CALLBACK, challenge)

        # Separate provider instance using the same public storage/key configuration. This checks
        # replacement, not database durability or atomic consumption by concurrent replicas.
        replacement = HandoffProbe(oidc_url=oidc_url, base_url=service_url, storage=storage, pending=pending)
        restored_client = await replacement.get_client(client_id)
        assert restored_client is not None
        assert restored_client.token_endpoint_auth_method == "none"
        assert restored_client.client_secret is None
        replacement_app = FastMCP("External consent probe", auth=replacement).http_app(path="/mcp")
        async with serve_app(replacement_app, port=service_port):
            # Stand-in for authenticated BFF completion: only the held, framework-produced URL
            # is followed. Browser binding and durable approval are deliberately not implemented.
            upstream = await browser.get(held.upstream_url)
            assert upstream.status_code == 302
            callback = await browser.get(upstream.headers["location"])
            assert callback.status_code == 302
            redirect = urlsplit(callback.headers["location"])
            assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == CALLBACK
            query = parse_qs(redirect.query)
            assert query["state"] == ["first-tab"]
            exchange = {
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": CALLBACK,
                "client_id": client_id,
                "code_verifier": verifier,
            }
            wrong_pkce = await browser.post(metadata["token_endpoint"], data=exchange | {"code_verifier": "wrong"})
            assert wrong_pkce.status_code == 401, wrong_pkce.text
            assert wrong_pkce.json()["error"] == "invalid_grant"
            token = await browser.post(metadata["token_endpoint"], data=exchange)
            assert token.status_code == 200, token.text
            verified = await replacement.load_access_token(token.json()["access_token"])
            assert verified is not None
            assert verified.client_id == client_id  # Shared adapter restores downstream identity.
            replay = await browser.post(metadata["token_endpoint"], data=exchange)
            assert replay.status_code == 401
            assert replay.json()["error"] == "invalid_grant"
            assert len(pending) == 2  # Protocol completion does not consume application consent state.


if __name__ == "__main__":
    pytest_bazel.main()
