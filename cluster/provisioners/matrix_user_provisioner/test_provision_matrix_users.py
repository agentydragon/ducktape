"""Tests for provision_matrix_users.py's two-phase Matrix/Synapse provisioning."""

import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import pytest_bazel

from cluster.provisioners.matrix_user_provisioner.provision_matrix_users import (
    ADMIN_DEVICE_ID,
    ADMIN_USERNAME,
    SERVER_NAME,
    SYNAPSE_URL,
    admin_login,
    register_admin,
    upsert_bot,
)

NONCE = "test-nonce"


@contextmanager
def _client(*responses: httpx.Response) -> Iterator[tuple[httpx.Client, list[httpx.Request]]]:
    queued = iter(responses)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(queued)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        yield client, calls


def test_register_admin_computes_hmac_and_posts_registration():
    with _client(
        httpx.Response(200, json={"nonce": NONCE}),
        httpx.Response(200, json={"user_id": f"@{ADMIN_USERNAME}:{SERVER_NAME}"}),
    ) as (client, calls):
        register_admin(client, "shared-secret", "adminpw")

        registration = calls[1]
        register_body = json.loads(registration.content)
        assert registration.method == "POST"
        assert str(registration.url) == f"{SYNAPSE_URL}/_synapse/admin/v1/register"
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
    with _client(
        httpx.Response(200, json={"nonce": NONCE}),
        httpx.Response(400, json={"errcode": "M_USER_IN_USE", "error": "already taken"}),
    ) as (client, _):
        with caplog.at_level(logging.INFO):
            register_admin(client, "shared-secret", "adminpw")  # must not raise

        assert "already exists" in caplog.text


def test_register_admin_raises_on_unexpected_error():
    with (
        _client(
            httpx.Response(200, json={"nonce": NONCE}),
            httpx.Response(500, json={"errcode": "M_UNKNOWN", "error": "boom"}),
        ) as (client, _),
        pytest.raises(httpx.HTTPStatusError),
    ):
        register_admin(client, "shared-secret", "adminpw")


@pytest.mark.parametrize(
    ("status", "lookup", "password"),
    [
        pytest.param(404, {"errcode": "M_NOT_FOUND"}, {"password": "botpw"}, id="missing"),
        pytest.param(200, {}, {"password": "botpw"}, id="missing-name"),
        pytest.param(200, {"name": "@test-bot:example.test"}, {}, id="existing-user-keeps-sessions"),
    ],
)
def test_upsert_bot_sets_password_only_when_creating(
    status: int, lookup: dict[str, str], password: dict[str, str]
) -> None:
    with _client(httpx.Response(status, json=lookup), httpx.Response(200, json={})) as (client, calls):
        upsert_bot(client, "admin-token", "botpw", bot_username="test-bot", bot_displayname="Test Bot")

    get, put = calls
    assert (get.method, put.method) == ("GET", "PUT")
    assert get.url == put.url
    assert put.url.raw_path == f"/_synapse/admin/v2/users/%40test-bot%3A{SERVER_NAME}".encode()
    assert all(call.headers["Authorization"] == "Bearer admin-token" for call in calls)
    assert json.loads(put.content) == {"displayname": "Test Bot", "admin": False, **password}


def test_upsert_bot_does_not_write_after_lookup_error() -> None:
    with (
        _client(httpx.Response(500, json={"errcode": "M_UNKNOWN"})) as (client, calls),
        pytest.raises(httpx.HTTPStatusError),
    ):
        upsert_bot(client, "admin-token", "botpw", bot_username="test-bot", bot_displayname="Test Bot")

    [get] = calls
    assert get.method == "GET"


def test_admin_login_pins_the_device_id():
    """Unpinned, every reconcile would leave another admin device behind — and
    clearing those with /logout/all is what revokes the bot's token."""
    with _client(httpx.Response(200, json={"access_token": "admin-token"})) as (client, calls):
        assert admin_login(client, "adminpw") == "admin-token"

        [login] = calls
        body = json.loads(login.content)
        assert login.method == "POST"
        assert str(login.url) == f"{SYNAPSE_URL}/_matrix/client/v3/login"
        assert body["device_id"] == ADMIN_DEVICE_ID


if __name__ == "__main__":
    pytest_bazel.main()
