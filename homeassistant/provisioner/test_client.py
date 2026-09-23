from http import HTTPStatus

import httpx2
import pytest
import pytest_bazel
import respx

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


if __name__ == "__main__":
    pytest_bazel.main()
