import json
from http import HTTPStatus

import httpx2
import pytest
import pytest_bazel
import respx
from httpx2.websockets import ASGIWebSocketTransport
from pydantic import ValidationError

from homeassistant.provisioner.client import HomeAssistantClient
from homeassistant.provisioner.endpoint import HomeAssistantEndpoint

# The httpx2_mock fixture comes from the auto-loaded pytest-httpx2 plugin.
# gazelle:include_dep @pypi//pytest_httpx2


@pytest.mark.parametrize(
    ("status", "valid"), [(HTTPStatus.OK, True), (HTTPStatus.UNAUTHORIZED, False), (HTTPStatus.FORBIDDEN, False)]
)
async def test_token_validity_is_home_assistants_answer(
    httpx2_mock: respx.Router, home_assistant_client, endpoint: HomeAssistantEndpoint, status, valid
):
    check = httpx2_mock.get(f"{endpoint.url}/api/").respond(status_code=status)

    assert await home_assistant_client.token_is_valid("held-token") is valid
    assert check.calls.last.request.headers["Authorization"] == "Bearer held-token"


async def test_an_unavailable_home_assistant_is_not_a_refused_token(
    httpx2_mock: respx.Router, home_assistant_client, endpoint: HomeAssistantEndpoint
):
    """An outage must fail the run, not replace a token Home Assistant may still accept."""
    httpx2_mock.get(f"{endpoint.url}/api/").respond(status_code=HTTPStatus.SERVICE_UNAVAILABLE)

    with pytest.raises(httpx2.HTTPStatusError):
        await home_assistant_client.token_is_valid("held-token")


async def test_websocket_command_authenticates_and_executes(endpoint: HomeAssistantEndpoint):
    received_messages: list[object] = []

    async def home_assistant(scope, receive, send):
        assert scope["type"] == "websocket"
        assert scope["path"] == "/api/websocket"
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": json.dumps({"type": "auth_required"})})
        auth_message = await receive()
        received_messages.append(json.loads(auth_message["text"]))
        await send({"type": "websocket.send", "text": json.dumps({"type": "auth_ok"})})
        command = await receive()
        received_messages.append(json.loads(command["text"]))
        await send(
            {"type": "websocket.send", "text": json.dumps({"type": "result", "success": True, "result": {"ok": True}})}
        )

    async with httpx2.AsyncClient(transport=ASGIWebSocketTransport(home_assistant)) as http_client:
        client = HomeAssistantClient(http_client, endpoint)
        client._access_token = "access-token"
        result = await client.websocket_command({"type": "http/config"})

    assert received_messages == [{"type": "auth", "access_token": "access-token"}, {"id": 1, "type": "http/config"}]
    assert result == {"ok": True}


@pytest.mark.parametrize(
    "response",
    [[{"step": "future_step", "done": False}], [{"step": "user", "done": 1}], {"step": "user", "done": True}],
)
async def test_pending_onboarding_steps_validates_response(
    httpx2_mock: respx.Router, home_assistant_client, endpoint: HomeAssistantEndpoint, response
):
    route = httpx2_mock.get(f"{endpoint.url}/api/onboarding").respond(json=response)

    with pytest.raises(ValidationError):
        await home_assistant_client.pending_onboarding_steps()
    assert route.called


if __name__ == "__main__":
    pytest_bazel.main()
