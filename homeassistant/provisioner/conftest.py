import httpx2
import pytest

from homeassistant.provisioner.client import HomeAssistantClient
from homeassistant.provisioner.endpoint import HomeAssistantEndpoint


@pytest.fixture
def endpoint() -> HomeAssistantEndpoint:
    return HomeAssistantEndpoint(
        url="http://home-assistant.test:8123", client_id="https://home.test/", redirect_uri="https://home.test/"
    )


@pytest.fixture
async def home_assistant_client(endpoint: HomeAssistantEndpoint):
    async with httpx2.AsyncClient() as http_client:
        yield HomeAssistantClient(http_client, endpoint)
