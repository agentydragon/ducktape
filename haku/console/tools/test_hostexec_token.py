"""Tests for HostexecJwtBearerExchanger — the per-host JWT-bearer token exchange, over a respx mock.

respx intercepts the token-endpoint POST, so the jwt-bearer exchange runs without a network hop.
"""

import httpx
import pytest
import pytest_bazel
import respx
from fastmcp.exceptions import ToolError

from haku.console.tools.hostexec_token import HostexecJwtBearerExchanger

TOKEN_ENDPOINT = "https://auth.allegedly.works/application/o/token/"
AUDIENCE_CLIENT_IDS = {"wyrm2": "hostexec-wyrm2-client", "rugged": "hostexec-rugged-client"}


@pytest.fixture
def exchanger() -> HostexecJwtBearerExchanger:
    return HostexecJwtBearerExchanger(
        operator_token="operator-authentik-token",
        token_endpoint=TOKEN_ENDPOINT,
        audience_client_ids=AUDIENCE_CLIENT_IDS,
        scope="openid",
    )


async def test_exchange_swaps_operator_token_for_per_host_audience(exchanger: HostexecJwtBearerExchanger) -> None:
    with respx.mock:
        route = respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"access_token": "per-host-token", "token_type": "bearer"})
        )
        token = await exchanger.exchange(host="wyrm2", run_as="root")
    assert token == "per-host-token"
    body = route.calls.last.request.content.decode()
    # jwt-bearer client assertion = the operator's token, exchanged against wyrm2's provider client.
    assert "grant_type=client_credentials" in body
    assert "client_assertion=operator-authentik-token" in body
    assert "client_id=hostexec-wyrm2-client" in body
    assert "urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer" in body


async def test_exchange_rejects_host_without_provider(exchanger: HostexecJwtBearerExchanger) -> None:
    with pytest.raises(ToolError, match="no configured hostexec Authentik provider"):
        await exchanger.exchange(host="atlas", run_as="root")


async def test_exchange_surfaces_authentik_rejection(exchanger: HostexecJwtBearerExchanger) -> None:
    with respx.mock:
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad assertion"})
        )
        with pytest.raises(ToolError, match=r"exchanging hostexec token for 'wyrm2' failed"):
            await exchanger.exchange(host="wyrm2", run_as="root")


async def test_exchange_rejects_empty_access_token(exchanger: HostexecJwtBearerExchanger) -> None:
    with respx.mock:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json={"token_type": "bearer"}))
        with pytest.raises(ToolError, match="returned no access_token"):
            await exchanger.exchange(host="wyrm2", run_as="root")


if __name__ == "__main__":
    pytest_bazel.main()
