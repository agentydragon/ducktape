import base64
from http import HTTPStatus

import httpx2
import kubernetes
import pytest
import pytest_bazel
import respx
from client import HomeAssistantClient
from settings import ProvisionerSettings, TokenConfig
from tokens import LIFESPAN_DAYS, provision_token, replace_long_lived_token

TOKEN = TokenConfig(
    client_name="test-consumer",
    secret_name="test-consumer-token",
    secret_namespace="test-namespace",
    description="Test consumer's token",
)


class FakeCoreV1(kubernetes.client.CoreV1Api):
    """The three Secret calls the provisioner makes, against at most one Secret."""

    def __init__(self, token: str | None) -> None:
        super().__init__()
        self.data = None if token is None else {"token": base64.b64encode(token.encode()).decode()}
        self.created: list[kubernetes.client.V1Secret] = []
        self.patched: list[kubernetes.client.V1Secret] = []

    def read_namespaced_secret(self, name, namespace, **kwargs):
        assert (name, namespace) == (TOKEN.secret_name, TOKEN.secret_namespace)
        if self.data is None:
            raise kubernetes.client.exceptions.ApiException(status=HTTPStatus.NOT_FOUND)
        return kubernetes.client.V1Secret(data=self.data)

    def create_namespaced_secret(self, namespace, body, **kwargs):
        assert namespace == TOKEN.secret_namespace
        self.created.append(body)

    def patch_namespaced_secret(self, name, namespace, body, **kwargs):
        assert (name, namespace) == (TOKEN.secret_name, TOKEN.secret_namespace)
        self.patched.append(body)


def fake_home_assistant_tokens(monkeypatch, home_assistant_client: HomeAssistantClient, tokens: list[dict]):
    """Answer the websocket token commands; return the commands sent, in order."""
    sent: list[dict[str, object]] = []

    async def websocket_command(message: dict[str, object]) -> object:
        sent.append(message)
        match message["type"]:
            case "auth/refresh_tokens":
                return tokens
            case "auth/delete_refresh_token":
                return None
            case "auth/long_lived_access_token":
                return "fresh-token"
        raise AssertionError(message)

    monkeypatch.setattr(home_assistant_client, "websocket_command", websocket_command)
    return sent


def mock_login(httpx2_mock: respx.Router, settings: ProvisionerSettings) -> respx.Route:
    httpx2_mock.post(f"{settings.home_assistant_url}/auth/login_flow").respond(json={"flow_id": "flow"})
    httpx2_mock.post(f"{settings.home_assistant_url}/auth/login_flow/flow").respond(json={"result": "code"})
    return httpx2_mock.post(f"{settings.home_assistant_url}/auth/token").respond(json={"access_token": "session"})


@pytest.mark.parametrize(
    ("status", "valid"), [(HTTPStatus.OK, True), (HTTPStatus.UNAUTHORIZED, False), (HTTPStatus.FORBIDDEN, False)]
)
async def test_token_validity_is_home_assistants_answer(
    httpx2_mock: respx.Router, home_assistant_client, provisioner_settings, status, valid
):
    check = httpx2_mock.get(f"{provisioner_settings.home_assistant_url}/api/").respond(status_code=status)

    assert await home_assistant_client.token_is_valid("held-token") is valid
    assert check.calls.last.request.headers["Authorization"] == "Bearer held-token"


async def test_an_unavailable_home_assistant_is_not_a_refused_token(
    httpx2_mock: respx.Router, home_assistant_client, provisioner_settings
):
    """An outage must fail the run, not replace a token Home Assistant may still accept."""
    httpx2_mock.get(f"{provisioner_settings.home_assistant_url}/api/").respond(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE
    )

    with pytest.raises(httpx2.HTTPStatusError):
        await home_assistant_client.token_is_valid("held-token")


async def test_replacing_revokes_same_named_long_lived_tokens_before_minting(monkeypatch, home_assistant_client):
    """Home Assistant refuses a second long-lived token of one name, so the stale one goes first."""
    sent = fake_home_assistant_tokens(
        monkeypatch,
        home_assistant_client,
        [
            {"id": "session", "type": "normal", "client_name": None},
            {"id": "other-consumer", "type": "long_lived_access_token", "client_name": "something-else"},
            {"id": "stale", "type": "long_lived_access_token", "client_name": TOKEN.client_name},
        ],
    )

    assert await replace_long_lived_token(home_assistant_client, TOKEN.client_name) == "fresh-token"
    assert sent == [
        {"id": 1, "type": "auth/refresh_tokens"},
        {"id": 1, "type": "auth/delete_refresh_token", "refresh_token_id": "stale"},
        {"id": 1, "type": "auth/long_lived_access_token", "client_name": TOKEN.client_name, "lifespan": LIFESPAN_DAYS},
    ]


async def test_an_accepted_token_is_left_alone(httpx2_mock: respx.Router, home_assistant_client, provisioner_settings):
    httpx2_mock.get(f"{provisioner_settings.home_assistant_url}/api/").respond(status_code=HTTPStatus.OK)
    v1 = FakeCoreV1("held-token")

    assert await provision_token(home_assistant_client, v1, TOKEN, "secret-password") is False
    assert (v1.created, v1.patched) == ([], [])


async def test_a_missing_secret_is_created_with_a_fresh_token(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, provisioner_settings
):
    mock_login(httpx2_mock, provisioner_settings)
    fake_home_assistant_tokens(monkeypatch, home_assistant_client, [])
    v1 = FakeCoreV1(None)

    assert await provision_token(home_assistant_client, v1, TOKEN, "secret-password") is True
    [created] = v1.created
    assert (created.metadata.name, created.metadata.namespace) == (TOKEN.secret_name, TOKEN.secret_namespace)
    assert created.metadata.annotations == {"description": TOKEN.description}
    assert created.string_data == {"token": "fresh-token"}
    assert v1.patched == []


async def test_a_refused_token_is_replaced_in_place(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, provisioner_settings
):
    httpx2_mock.get(f"{provisioner_settings.home_assistant_url}/api/").respond(status_code=HTTPStatus.UNAUTHORIZED)
    login = mock_login(httpx2_mock, provisioner_settings)
    fake_home_assistant_tokens(monkeypatch, home_assistant_client, [])
    v1 = FakeCoreV1("revoked-token")

    assert await provision_token(home_assistant_client, v1, TOKEN, "secret-password") is True
    assert login.called
    assert [secret.string_data for secret in v1.patched] == [{"token": "fresh-token"}]
    assert v1.created == []


if __name__ == "__main__":
    pytest_bazel.main()
