"""Tests for provision_matrix_users.py's two-phase Matrix/Synapse provisioning."""

import hashlib
import hmac

import httpx
import pytest
import pytest_bazel

from cluster.provisioners.matrix_user_provisioner.provision_matrix_users import (
    ADMIN_USERNAME,
    BOT_DISPLAYNAME,
    BOT_USERNAME,
    SERVER_NAME,
    SYNAPSE_URL,
    _bot_exists,
    register_admin,
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


def test_register_admin_skips_when_already_registered(capsys: pytest.CaptureFixture):
    client = _FakeClient(
        [_response(200, {"nonce": NONCE}), _response(400, {"errcode": "M_USER_IN_USE", "error": "already taken"})]
    )

    register_admin(client, "shared-secret", "adminpw")  # must not raise

    assert "already exists" in capsys.readouterr().out


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
            _response(200, {"access_token": "admin-token"}),  # login
            _response(200, {}),  # bot does not exist yet
            _response(200, {"displayname": BOT_DISPLAYNAME}),  # PUT
        ]
    )

    upsert_bot(client, "adminpw", "botpw")

    [(_, _, put_body)] = [c for c in client.calls if c[0] == "PUT"]
    assert put_body == {"password": "botpw", "displayname": BOT_DISPLAYNAME, "admin": False}


def test_upsert_bot_updates_without_password_when_present():
    client = _FakeClient(
        [
            _response(200, {"access_token": "admin-token"}),  # login
            _response(200, {"name": f"@{BOT_USERNAME}:{SERVER_NAME}"}),  # bot exists
            _response(200, {"displayname": BOT_DISPLAYNAME}),  # PUT
        ]
    )

    upsert_bot(client, "adminpw", "botpw")

    [(_, _, put_body)] = [c for c in client.calls if c[0] == "PUT"]
    assert put_body == {"displayname": BOT_DISPLAYNAME, "admin": False}


if __name__ == "__main__":
    pytest_bazel.main()
