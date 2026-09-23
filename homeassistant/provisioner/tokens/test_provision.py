import base64
from http import HTTPStatus

import kubernetes
import pytest_bazel
import respx

from homeassistant.provisioner.client import HomeAssistantClient
from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.tokens.provision import (
    LIFESPAN_DAYS,
    READ_ONLY_GROUP,
    provision_token,
    replace_long_lived_token,
    reset_read_only_user,
)
from homeassistant.provisioner.tokens.settings import TokenConfig

# The httpx2_mock fixture comes from the auto-loaded pytest-httpx2 plugin.
# gazelle:include_dep @pypi//pytest_httpx2

OWNER = "test-admin"
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


def mock_login(httpx2_mock: respx.Router, endpoint: HomeAssistantEndpoint) -> respx.Route:
    httpx2_mock.post(f"{endpoint.url}/auth/login_flow").respond(json={"flow_id": "flow"})
    httpx2_mock.post(f"{endpoint.url}/auth/login_flow/flow").respond(json={"result": "code"})
    return httpx2_mock.post(f"{endpoint.url}/auth/token").respond(json={"access_token": "session"})


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


async def test_an_accepted_token_is_left_alone(httpx2_mock: respx.Router, home_assistant_client, endpoint):
    httpx2_mock.get(f"{endpoint.url}/api/").respond(status_code=HTTPStatus.OK)
    v1 = FakeCoreV1("held-token")

    assert await provision_token(home_assistant_client, v1, TOKEN, OWNER, "secret-password") is False
    assert (v1.created, v1.patched) == ([], [])


async def test_a_missing_secret_is_created_with_a_fresh_token(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, endpoint
):
    mock_login(httpx2_mock, endpoint)
    fake_home_assistant_tokens(monkeypatch, home_assistant_client, [])
    v1 = FakeCoreV1(None)

    assert await provision_token(home_assistant_client, v1, TOKEN, OWNER, "secret-password") is True
    [created] = v1.created
    assert (created.metadata.name, created.metadata.namespace) == (TOKEN.secret_name, TOKEN.secret_namespace)
    assert created.metadata.annotations == {"description": TOKEN.description}
    assert created.string_data == {"token": "fresh-token"}
    assert v1.patched == []


async def test_a_refused_token_is_replaced_in_place(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, endpoint
):
    httpx2_mock.get(f"{endpoint.url}/api/").respond(status_code=HTTPStatus.UNAUTHORIZED)
    login = mock_login(httpx2_mock, endpoint)
    fake_home_assistant_tokens(monkeypatch, home_assistant_client, [])
    v1 = FakeCoreV1("revoked-token")

    assert await provision_token(home_assistant_client, v1, TOKEN, OWNER, "secret-password") is True
    assert login.called
    assert [secret.string_data for secret in v1.patched] == [{"token": "fresh-token"}]
    assert v1.created == []


def fake_owner_users(monkeypatch, home_assistant_client: HomeAssistantClient, users: list[dict]):
    """Answer the owner's user-management commands; return the commands sent, in order."""
    sent: list[dict[str, object]] = []

    async def websocket_command(message: dict[str, object]) -> object:
        sent.append(message)
        match message["type"]:
            case "config/auth/list":
                return users
            case "config/auth/create":
                return {"user": {"id": "created-user"}}
        return None

    monkeypatch.setattr(home_assistant_client, "websocket_command", websocket_command)
    return sent


async def test_a_missing_read_only_user_is_created_local_only_in_the_read_only_group(
    monkeypatch, home_assistant_client
):
    sent = fake_owner_users(monkeypatch, home_assistant_client, [{"id": "someone", "username": "someone-else"}])

    await reset_read_only_user(home_assistant_client, "reader", "fresh-password")

    assert sent == [
        {"id": 1, "type": "config/auth/list"},
        {"id": 1, "type": "config/auth/create", "name": "reader", "group_ids": [READ_ONLY_GROUP], "local_only": True},
        {
            "id": 1,
            "type": "config/auth_provider/homeassistant/create",
            "user_id": "created-user",
            "username": "reader",
            "password": "fresh-password",
        },
    ]


async def test_an_existing_read_only_user_is_confined_again_and_given_the_new_password(
    monkeypatch, home_assistant_client
):
    """Whatever groups the user gained since, it ends in the read-only group alone."""
    sent = fake_owner_users(monkeypatch, home_assistant_client, [{"id": "reader-id", "username": "reader"}])

    await reset_read_only_user(home_assistant_client, "reader", "fresh-password")

    assert sent == [
        {"id": 1, "type": "config/auth/list"},
        {
            "id": 1,
            "type": "config/auth/update",
            "user_id": "reader-id",
            "group_ids": [READ_ONLY_GROUP],
            "local_only": True,
        },
        {
            "id": 1,
            "type": "config/auth_provider/homeassistant/admin_change_password",
            "user_id": "reader-id",
            "password": "fresh-password",
        },
    ]


async def test_a_read_only_users_token_is_minted_in_that_users_session(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, endpoint
):
    """The owner only resets the user; a token the owner minted would carry the owner's authority."""
    url = endpoint.url
    httpx2_mock.post(f"{url}/auth/login_flow").respond(json={"flow_id": "flow"})
    for username in (OWNER, "reader"):
        httpx2_mock.post(f"{url}/auth/login_flow/flow", json__username=username).respond(json={"result": username})
        httpx2_mock.post(
            f"{url}/auth/token",
            data={"grant_type": "authorization_code", "code": username, "client_id": endpoint.client_id},
        ).respond(json={"access_token": f"session-of-{username}"})
    sent: list[tuple[str | None, str]] = []

    async def websocket_command(self: HomeAssistantClient, message: dict[str, object]) -> object:
        sent.append((self._access_token, str(message["type"])))
        match message["type"]:
            case "config/auth/list" | "auth/refresh_tokens":
                return []
            case "config/auth/create":
                return {"user": {"id": "reader-id"}}
            case "auth/long_lived_access_token":
                return "reader-token"
        return None

    monkeypatch.setattr(HomeAssistantClient, "websocket_command", websocket_command)
    token = TOKEN.model_copy(update={"read_only_user": "reader"})
    v1 = FakeCoreV1(None)

    assert await provision_token(home_assistant_client, v1, token, OWNER, "secret-password") is True
    owner = f"session-of-{OWNER}"
    assert sent == [
        (owner, "config/auth/list"),
        (owner, "config/auth/create"),
        (owner, "config/auth_provider/homeassistant/create"),
        ("session-of-reader", "auth/refresh_tokens"),
        ("session-of-reader", "auth/long_lived_access_token"),
    ]
    assert [secret.string_data for secret in v1.created] == [{"token": "reader-token"}]


if __name__ == "__main__":
    pytest_bazel.main()
