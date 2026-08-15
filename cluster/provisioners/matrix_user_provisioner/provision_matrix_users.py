"""Provision Matrix users on Synapse: the provisioner admin + agent bots.

Two-phase idempotent provisioning:
  Phase 1: Register provisioner as admin via shared-secret endpoint.
           Skips if user already exists (M_USER_IN_USE).
  Phase 2: Log in as provisioner, then ensure the bot user exists via Synapse
           admin API. Only sets password on initial creation — setting password
           on an existing user invalidates all access tokens (Synapse re-hashes
           with bcrypt and deletes all devices/tokens), which breaks the bot's
           active Matrix session.

This deliberately does **not** mint an access token for the bot. haku-console
holds the bot password and logs in for itself, so it can replace its own token
the moment Synapse stops accepting one, rather than waiting for this Job to run
again. See haku/plans/matrix_chat_runtime.md R10.3.

Requires: REGISTRATION_SECRET, ADMIN_PASSWORD, BOT_PASSWORD, and one password
environment variable per bot in BOT_SPECS.
"""

import hashlib
import hmac
import logging
import os
import urllib.parse
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

SYNAPSE_URL = "http://matrix-synapse.matrix.svc.cluster.local:8008"
ADMIN_USERNAME = "provisioner"
ADMIN_DEVICE_ID = "matrix-user-provisioner"
BOT_USERNAME = "haku"
BOT_DISPLAYNAME = "Haku"
PUBLIC_CODER_AGENT_BOT_USERNAME = "public-coder-agent"
PUBLIC_CODER_AGENT_BOT_DISPLAYNAME = "Public Coder Agent"
SERVER_NAME = "allegedly.works"

BOT_SPECS = (
    (BOT_USERNAME, BOT_DISPLAYNAME, "BOT_PASSWORD"),
    (PUBLIC_CODER_AGENT_BOT_USERNAME, PUBLIC_CODER_AGENT_BOT_DISPLAYNAME, "PUBLIC_CODER_AGENT_BOT_PASSWORD"),
)


class SynapseClient(Protocol):
    """The subset of httpx.Client's interface this module needs — lets tests
    inject a lightweight fake without subclassing httpx.Client."""

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response: ...
    def post(self, url: str, *, json: dict | None = None) -> httpx.Response: ...
    def put(self, url: str, *, json: dict | None = None, headers: dict[str, str] | None = None) -> httpx.Response: ...


def register_admin(client: SynapseClient, registration_secret: str, admin_password: str) -> None:
    """Phase 1: Register provisioner as admin via shared-secret."""
    register_url = f"{SYNAPSE_URL}/_synapse/admin/v1/register"
    nonce = client.get(register_url).json()["nonce"]

    # HMAC-SHA1: nonce\0username\0password\0admin
    mac_input = f"{nonce}\0{ADMIN_USERNAME}\0{admin_password}\0admin"
    mac = hmac.new(registration_secret.encode(), mac_input.encode(), hashlib.sha1).hexdigest()

    resp = client.post(
        register_url,
        json={"nonce": nonce, "username": ADMIN_USERNAME, "password": admin_password, "admin": True, "mac": mac},
    )
    if resp.status_code == 400 and resp.json().get("errcode") == "M_USER_IN_USE":
        logger.info("Phase 1: Admin @%s:%s already exists, skipping", ADMIN_USERNAME, SERVER_NAME)
        return
    resp.raise_for_status()
    logger.info("Phase 1: Registered admin @%s", resp.json()["user_id"])


def admin_login(client: SynapseClient, admin_password: str) -> str:
    """Log in as the provisioner admin and return its access token.

    Pins `device_id` so re-runs reuse one device instead of leaving a new one
    behind each time. This Job is recreated on every Flux reconcile, so an
    unpinned login would accumulate admin devices indefinitely.
    """
    resp = client.post(
        f"{SYNAPSE_URL}/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": ADMIN_USERNAME},
            "password": admin_password,
            "device_id": ADMIN_DEVICE_ID,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def _admin_user_url(encoded_mxid: str) -> str:
    return f"{SYNAPSE_URL}/_synapse/admin/v2/users/{encoded_mxid}"


def _bot_exists(client: SynapseClient, access_token: str, encoded_mxid: str) -> bool:
    """Check if bot user already exists in Synapse."""
    resp = client.get(_admin_user_url(encoded_mxid), headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    # Synapse returns the user object even for non-existent users but with
    # no "name" field. A real user always has "name".
    return "name" in resp.json()


def upsert_bot(
    client: SynapseClient,
    admin_token: str,
    bot_password: str,
    *,
    bot_username: str = BOT_USERNAME,
    bot_displayname: str = BOT_DISPLAYNAME,
) -> None:
    """Phase 2: Ensure the bot user exists via the admin API."""
    bot_mxid = f"@{bot_username}:{SERVER_NAME}"
    encoded_mxid = urllib.parse.quote(bot_mxid)
    auth = {"Authorization": f"Bearer {admin_token}"}
    url = _admin_user_url(encoded_mxid)

    if _bot_exists(client, admin_token, encoded_mxid):
        # User exists — update displayname only. Do NOT include password:
        # Synapse invalidates all access tokens on password change, even if
        # the value is identical (bcrypt rehash triggers device purge), which
        # would log haku-console out on every reconcile.
        resp = client.put(url, json={"displayname": bot_displayname, "admin": False}, headers=auth)
        resp.raise_for_status()
        logger.info("Phase 2: Updated %s (displayname: %s)", bot_mxid, resp.json().get("displayname", "n/a"))
    else:
        resp = client.put(
            url, json={"password": bot_password, "displayname": bot_displayname, "admin": False}, headers=auth
        )
        resp.raise_for_status()
        logger.info("Phase 2: Created %s (displayname: %s)", bot_mxid, resp.json().get("displayname", "n/a"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    registration_secret = os.environ["REGISTRATION_SECRET"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    with httpx.Client(timeout=30) as client:
        register_admin(client, registration_secret, admin_password)
        admin_token = admin_login(client, admin_password)
        logger.info("Logged in as @%s:%s", ADMIN_USERNAME, SERVER_NAME)
        for bot_username, bot_displayname, password_env in BOT_SPECS:
            upsert_bot(
                client,
                admin_token,
                os.environ[password_env],
                bot_username=bot_username,
                bot_displayname=bot_displayname,
            )
    logger.info("Done: all Matrix users provisioned")


if __name__ == "__main__":
    main()
