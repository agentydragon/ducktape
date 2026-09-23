import httpx2
import pytest

from homeassistant.provisioner.client import HomeAssistantClient
from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.settings import ComponentConfig, HttpConfig, ProvisionerSettings


@pytest.fixture
def endpoint() -> HomeAssistantEndpoint:
    return HomeAssistantEndpoint(
        url="http://home-assistant.test:8123", client_id="https://home.test/", redirect_uri="https://home.test/"
    )


@pytest.fixture
def provisioner_settings(endpoint: HomeAssistantEndpoint) -> ProvisionerSettings:
    return ProvisionerSettings(
        home_assistant_url=endpoint.url,
        client_id=endpoint.client_id,
        redirect_uri=endpoint.redirect_uri,
        username="test-admin",
        display_name="Test Administrator",
        local_admin_password="secret-password",
        http_config=HttpConfig(
            server_host=["127.0.0.1"],
            server_port=8124,
            cors_allowed_origins=["https://cast.test"],
            use_x_forwarded_for=True,
            trusted_proxies=["127.0.0.1/32"],
            login_attempts_threshold=-1,
            ip_ban_enabled=True,
            ssl_profile="modern",
            use_x_frame_options=True,
        ),
        components=(
            ComponentConfig(
                version="2.2.1",
                url="https://example.test/component.zip",
                sha256="0" * 64,
                archive_path="*/custom_components/example",
                install_dir="example",
                manifest_domain="example",
            ),
            ComponentConfig(
                version="1.1.1",
                url="https://example.test/root-component.zip",
                sha256="0" * 64,
                archive_path=".",
                install_dir="root_component",
                manifest_domain=None,
                config_files=("automations.yaml", "scripts.yaml", "scenes.yaml"),
            ),
        ),
        onboarding_enabled=True,
    )


@pytest.fixture
async def home_assistant_client(endpoint: HomeAssistantEndpoint):
    async with httpx2.AsyncClient() as http_client:
        yield HomeAssistantClient(http_client, endpoint)
