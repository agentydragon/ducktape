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
    PUBLIC_CODER_AGENT_BOT_DISPLAYNAME,
    PUBLIC_CODER_AGENT_BOT_USERNAME,
    SERVER_NAME,
    SYNAPSE_URL,
    _bot_exists,
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


def test_bot_exists_false_on_404():
    with _client(httpx.Response(404, json={"errcode": "M_NOT_FOUND"})) as (client, _):
        assert _bot_exists(client, "token", "encoded-mxid") is False


def test_bot_exists_reraises_non_404_errors():
    with (
        _client(httpx.Response(500, json={"errcode": "M_UNKNOWN"})) as (client, _),
        pytest.raises(httpx.HTTPStatusError),
    ):
        _bot_exists(client, "token", "encoded-mxid")


def test_upsert_bot_creates_with_password_when_absent():
    with _client(
        httpx.Response(200, json={}), httpx.Response(200, json={"displayname": PUBLIC_CODER_AGENT_BOT_DISPLAYNAME})
    ) as (client, calls):
        upsert_bot(
            client,
            "admin-token",
            "botpw",
            bot_username=PUBLIC_CODER_AGENT_BOT_USERNAME,
            bot_displayname=PUBLIC_CODER_AGENT_BOT_DISPLAYNAME,
        )

        [put] = [call for call in calls if call.method == "PUT"]
        put_body = json.loads(put.content)
        assert str(put.url).endswith(f"%40{PUBLIC_CODER_AGENT_BOT_USERNAME}%3A{SERVER_NAME}")
        assert all(call.headers["Authorization"] == "Bearer admin-token" for call in calls)
        assert put_body == {"password": "botpw", "displayname": PUBLIC_CODER_AGENT_BOT_DISPLAYNAME, "admin": False}


def test_upsert_bot_updates_without_password_when_present():
    """Re-running must not resend the password: Synapse purges every device and
    access token on a password set, even to an identical value."""
    with _client(
        httpx.Response(200, json={"name": f"@{PUBLIC_CODER_AGENT_BOT_USERNAME}:{SERVER_NAME}"}),
        httpx.Response(200, json={"displayname": PUBLIC_CODER_AGENT_BOT_DISPLAYNAME}),
    ) as (client, calls):
        upsert_bot(
            client,
            "admin-token",
            "botpw",
            bot_username=PUBLIC_CODER_AGENT_BOT_USERNAME,
            bot_displayname=PUBLIC_CODER_AGENT_BOT_DISPLAYNAME,
        )

        [put] = [call for call in calls if call.method == "PUT"]
        put_body = json.loads(put.content)
        assert all(call.headers["Authorization"] == "Bearer admin-token" for call in calls)
        assert put_body == {"displayname": PUBLIC_CODER_AGENT_BOT_DISPLAYNAME, "admin": False}


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
