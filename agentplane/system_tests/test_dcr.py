"""Deployed MCP edge discovery and registration through its real port-forward."""

import asyncio
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_bazel

from agentplane.system_tests.conftest import MCP_EDGE_URL


def _edge_url(value: object, server: str) -> str:
    assert isinstance(value, str)
    endpoint, edge = urlsplit(value), urlsplit(server)
    assert (endpoint.scheme, endpoint.hostname, endpoint.port) == (edge.scheme, edge.hostname, edge.port)
    assert endpoint.username is None
    assert endpoint.password is None
    assert not endpoint.query
    assert not endpoint.fragment
    return value


def _document(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers.get("content-type", "").split(";")[0] == "application/json"
    document = response.json()
    assert isinstance(document, dict)
    return document


async def test_deployed_discovery_and_registration(pytestconfig: pytest.Config) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    server = os.environ[MCP_EDGE_URL]
    phase = "MCP challenge"
    try:
        async with asyncio.timeout(60), httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            challenge = await client.post(
                server,
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test-deployed-discovery", "version": "1"},
                    },
                },
            )
            assert challenge.status_code == 401
            authenticate = challenge.headers["www-authenticate"]
            assert authenticate.split(" ", 1)[0].lower() == "bearer"
            advertised = re.search(r'\bresource_metadata="([^"]+)"', authenticate)
            assert advertised is not None

            phase = "protected-resource metadata"
            resource = _document(await client.get(_edge_url(advertised[1], server)))
            assert resource["resource"] == server
            issuers = resource["authorization_servers"]
            assert isinstance(issuers, list)
            assert issuers
            issuer = _edge_url(issuers[0], server)
            assert not urlsplit(issuer).path

            phase = "authorization-server metadata"
            metadata = _document(await client.get(issuer + "/.well-known/oauth-authorization-server"))
            assert metadata["issuer"] == issuer
            registration_endpoint = _edge_url(metadata["registration_endpoint"], server)

            phase = "registration"
            redirect_uris = ["http://127.0.0.1:49152/callback"]
            registered = _document(
                await client.post(
                    registration_endpoint,
                    json={
                        "client_name": "test-deployed-discovery",
                        "redirect_uris": redirect_uris,
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    },
                ),
                status=201,
            )
            assert isinstance(registered["client_id"], str)
            assert registered["client_id"]
            assert registered["redirect_uris"] == redirect_uris
            assert registered["token_endpoint_auth_method"] == "none"
            assert registered.get("client_secret") is None
    except Exception as error:
        # HTTP errors and JSON/assertion diagnostics can contain client credentials.
        pytest.fail(f"Deployed DCR failed at {phase}: {type(error).__name__}; HTTP details withheld", pytrace=False)


if __name__ == "__main__":
    pytest_bazel.main()
