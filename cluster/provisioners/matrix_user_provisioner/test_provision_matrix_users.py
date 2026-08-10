"""Tests for provision_matrix_users.py's Matrix/Synapse provisioning and token upkeep."""

import base64
import hashlib
import hmac
import logging

import httpx
import pytest
import pytest_bazel
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from cluster.provisioners.matrix_user_provisioner.provision_matrix_users import (
    ADMIN_DEVICE_ID,
    ADMIN_USERNAME,
    BOT_DISPLAYNAME,
    BOT_USERNAME,
    SERVER_NAME,
    SYNAPSE_URL,
    TOKEN_SECRET_KEY,
    _bot_exists,
    admin_login,
    ensure_bot_token,
    mint_bot_token,
    register_admin,
    token_is_valid,
    upsert_bot,
)

NONCE = "test-nonce"


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("GET", "http://x"))


class _FakeClient:
    """Returns canned responses in call order; records each call as (method, url, json body)."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = iter(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def _record(self, method: str, url: str, json: dict | None) -> httpx.Response:
        self.calls.append((method, url, json))
        return next(self._responses)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._record("GET", url, None)

    def post(self, url: str, json: dict | None = None, **kwargs) -> httpx.Response:
        return self._record("POST", url, json)

    def put(self, url: str, json: dict | None = None, **kwargs) -> httpx.Response:
        return self._record("PUT", url, json)


def test_register_admin_computes_hmac_and_posts_registration():
    client = _FakeClient(
        [_response(200, {"nonce": NONCE}), _response(200, {"user_id": f"@{ADMIN_USERNAME}:{SERVER_NAME}"})]
    )

    register_admin(client, "shared-secret", "adminpw")

    _, url, register_body = client.calls[1]
    assert url == f"{SYNAPSE_URL}/_synapse/admin/v1/register"
    assert register_body is not None
    expected_mac = hmac.new(
        b"shared-secret", f"{NONCE}\0{ADMIN_USERNAME}\0adminpw\0admin".encode(), hashlib.sha1
    ).hexdigest()
    assert register_body == {
        "nonce": NONCE,
        "username": ADMIN_USERNAME,
        "password": "adminpw",
        "admin": True,
        "mac": expected_mac,
    }


def test_register_admin_skips_when_already_registered(caplog: pytest.LogCaptureFixture):
    client = _FakeClient(
        [_response(200, {"nonce": NONCE}), _response(400, {"errcode": "M_USER_IN_USE", "error": "already taken"})]
    )

    with caplog.at_level(logging.INFO):
        register_admin(client, "shared-secret", "adminpw")  # must not raise

    assert "already exists" in caplog.text


def test_register_admin_raises_on_unexpected_error():
    client = _FakeClient([_response(200, {"nonce": NONCE}), _response(500, {"errcode": "M_UNKNOWN", "error": "boom"})])

    with pytest.raises(httpx.HTTPStatusError):
        register_admin(client, "shared-secret", "adminpw")


def test_bot_exists_true_when_response_has_name():
    client = _FakeClient([_response(200, {"name": f"@{BOT_USERNAME}:{SERVER_NAME}"})])
    assert _bot_exists(client, "token", "encoded-mxid") is True


def test_bot_exists_false_when_response_has_no_name():
    client = _FakeClient([_response(200, {})])
    assert _bot_exists(client, "token", "encoded-mxid") is False


def test_bot_exists_false_on_404():
    client = _FakeClient([_response(404, {"errcode": "M_NOT_FOUND"})])
    assert _bot_exists(client, "token", "encoded-mxid") is False


def test_bot_exists_reraises_non_404_errors():
    client = _FakeClient([_response(500, {"errcode": "M_UNKNOWN"})])
    with pytest.raises(httpx.HTTPStatusError):
        _bot_exists(client, "token", "encoded-mxid")


def test_upsert_bot_creates_with_password_when_absent():
    client = _FakeClient(
        [
            _response(200, {}),  # bot does not exist yet
            _response(200, {"displayname": BOT_DISPLAYNAME}),  # PUT
        ]
    )

    upsert_bot(client, "admin-token", "botpw")

    [(_, _, put_body)] = [c for c in client.calls if c[0] == "PUT"]
    assert put_body == {"password": "botpw", "displayname": BOT_DISPLAYNAME, "admin": False}


def test_upsert_bot_updates_without_password_when_present():
    """Re-running must not resend the password: Synapse purges every device and
    access token on a password set, even to an identical value."""
    client = _FakeClient(
        [
            _response(200, {"name": f"@{BOT_USERNAME}:{SERVER_NAME}"}),  # bot exists
            _response(200, {"displayname": BOT_DISPLAYNAME}),  # PUT
        ]
    )

    upsert_bot(client, "admin-token", "botpw")

    [(_, _, put_body)] = [c for c in client.calls if c[0] == "PUT"]
    assert put_body == {"displayname": BOT_DISPLAYNAME, "admin": False}


def test_admin_login_pins_the_device_id():
    """Unpinned, every reconcile would leave another admin device behind — and
    clearing those with /logout/all is what revokes the bot's token."""
    client = _FakeClient([_response(200, {"access_token": "admin-token"})])

    assert admin_login(client, "adminpw") == "admin-token"

    [(_, _, body)] = client.calls
    assert body is not None
    assert body["device_id"] == ADMIN_DEVICE_ID


def test_token_is_valid_true_for_the_bots_own_token():
    client = _FakeClient([_response(200, {"user_id": f"@{BOT_USERNAME}:{SERVER_NAME}"})])
    assert token_is_valid(client, "tok") is True


def test_token_is_valid_false_when_unauthorized():
    client = _FakeClient([_response(401, {"errcode": "M_UNKNOWN_TOKEN"})])
    assert token_is_valid(client, "stale") is False


def test_token_is_valid_false_when_token_belongs_to_another_user():
    """A token that authenticates as somebody else is not a usable bot token."""
    client = _FakeClient([_response(200, {"user_id": f"@someone-else:{SERVER_NAME}"})])
    assert token_is_valid(client, "tok") is False


def test_mint_bot_token_uses_admin_login_as_user():
    client = _FakeClient([_response(200, {"access_token": "fresh"})])

    assert mint_bot_token(client, "admin-token") == "fresh"

    [(method, url, _)] = client.calls
    assert method == "POST"
    assert url.endswith(f"/_synapse/admin/v1/users/%40{BOT_USERNAME}%3A{SERVER_NAME}/login")


class _FakeSecretStore:
    """Records secret writes; raises 404 like the real client for a missing secret."""

    def __init__(self, existing: str | None = None):
        self.existing = existing
        self.written: str | None = None

    def read_namespaced_secret(self, name: str, namespace: str) -> k8s.V1Secret:
        if self.existing is None:
            raise ApiException(status=404)
        return k8s.V1Secret(data={TOKEN_SECRET_KEY: base64.b64encode(self.existing.encode()).decode()})

    def patch_namespaced_secret(self, name: str, namespace: str, body: k8s.V1Secret) -> k8s.V1Secret:
        self.written = body.string_data[TOKEN_SECRET_KEY]
        return body

    def create_namespaced_secret(self, namespace: str, body: k8s.V1Secret) -> k8s.V1Secret:
        self.written = body.string_data[TOKEN_SECRET_KEY]
        return body


def test_ensure_bot_token_keeps_a_working_token():
    client = _FakeClient([_response(200, {"user_id": f"@{BOT_USERNAME}:{SERVER_NAME}"})])
    v1 = _FakeSecretStore(existing="still-good")

    ensure_bot_token(client, v1, "admin-token")

    assert v1.written is None


def test_ensure_bot_token_mints_when_the_stored_one_is_stale():
    client = _FakeClient([_response(401, {"errcode": "M_UNKNOWN_TOKEN"}), _response(200, {"access_token": "fresh"})])
    v1 = _FakeSecretStore(existing="stale")

    ensure_bot_token(client, v1, "admin-token")

    assert v1.written == "fresh"


def test_ensure_bot_token_mints_when_no_secret_exists():
    client = _FakeClient([_response(200, {"access_token": "fresh"})])
    v1 = _FakeSecretStore(existing=None)

    ensure_bot_token(client, v1, "admin-token")

    assert v1.written == "fresh"


if __name__ == "__main__":
    pytest_bazel.main()
