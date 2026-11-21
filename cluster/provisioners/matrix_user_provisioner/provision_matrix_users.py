"""Provision the Matrix admin and Public Coder Agent bot on Synapse.

Idempotent. The password is set only on initial creation: setting it on an existing user
deletes every device and access token, which would break the bot's live session.

The bot's consumer logs in directly and owns its access token.

Requires REGISTRATION_SECRET, ADMIN_PASSWORD, and PUBLIC_CODER_AGENT_BOT_PASSWORD.
"""

import hashlib
import hmac
import logging
import os
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

SYNAPSE_URL = "http://matrix-synapse.matrix.svc.cluster.local:8008"
ADMIN_USERNAME = "provisioner"
ADMIN_DEVICE_ID = "matrix-user-provisioner"
SERVER_NAME = "allegedly.works"


def register_admin(client: httpx.Client, registration_secret: str, admin_password: str) -> None:
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


def admin_login(client: httpx.Client, admin_password: str) -> str:
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


def upsert_bot(
    client: httpx.Client, admin_token: str, bot_password: str, *, bot_username: str, bot_displayname: str
) -> None:
    """Phase 2: Ensure the bot user exists via the admin API."""
    bot_mxid = f"@{bot_username}:{SERVER_NAME}"
    url = f"{SYNAPSE_URL}/_synapse/admin/v2/users/{urllib.parse.quote(bot_mxid)}"
    auth = {"Authorization": f"Bearer {admin_token}"}

    lookup = client.get(url, headers=auth)
    # A missing user can also be represented by a successful response without a name.
    exists = lookup.status_code != 404 and "name" in lookup.raise_for_status().json()
    body = {"displayname": bot_displayname, "admin": False}
    # Setting the password on an existing user purges their devices and access tokens.
    if not exists:
        body["password"] = bot_password
    response = client.put(url, json=body, headers=auth).raise_for_status()
    logger.info(
        "Phase 2: %s %s (displayname: %s)",
        "Updated" if exists else "Created",
        bot_mxid,
        response.json().get("displayname", "n/a"),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    registration_secret = os.environ["REGISTRATION_SECRET"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    with httpx.Client(timeout=30) as client:
        register_admin(client, registration_secret, admin_password)
        admin_token = admin_login(client, admin_password)
        logger.info("Logged in as @%s:%s", ADMIN_USERNAME, SERVER_NAME)
        upsert_bot(
            client,
            admin_token,
            os.environ["PUBLIC_CODER_AGENT_BOT_PASSWORD"],
            bot_username="public-coder-agent",
            bot_displayname="Public Coder Agent",
        )
    logger.info("Done: all Matrix users provisioned")


if __name__ == "__main__":
    main()
