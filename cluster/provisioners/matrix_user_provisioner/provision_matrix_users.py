"""Provision Matrix users on Synapse: admin + OpenClaw bot.

Two-phase idempotent provisioning:
  Phase 1: Register provisioner as admin via shared-secret endpoint.
           Skips if user already exists (M_USER_IN_USE).
  Phase 2: Log in as provisioner, then ensure the bot user exists via Synapse
           admin API. Only sets password on initial creation — setting password
           on an existing user invalidates all access tokens (Synapse re-hashes
           with bcrypt and deletes all devices/tokens), which breaks the bot's
           active Matrix session.

Requires: REGISTRATION_SECRET, ADMIN_PASSWORD, BOT_PASSWORD env vars.
"""

import hashlib
import hmac
import os
import urllib.parse

import httpx

SYNAPSE_URL = "http://matrix-synapse.matrix.svc.cluster.local:8008"
ADMIN_USERNAME = "provisioner"
BOT_USERNAME = "openclaw"
BOT_DISPLAYNAME = "OpenClaw"
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
        print(f"Phase 1: Admin @{ADMIN_USERNAME}:{SERVER_NAME} already exists, skipping")
        return
    resp.raise_for_status()
    print(f"Phase 1: Registered admin @{resp.json()['user_id']}")


def _admin_user_url(encoded_mxid: str) -> str:
    return f"{SYNAPSE_URL}/_synapse/admin/v2/users/{encoded_mxid}"


def _bot_exists(client: httpx.Client, access_token: str, encoded_mxid: str) -> bool:
    """Check if bot user already exists in Synapse."""
    resp = client.get(_admin_user_url(encoded_mxid), headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    # Synapse returns the user object even for non-existent users but with
    # no "name" field. A real user always has "name".
    return "name" in resp.json()


def upsert_bot(client: httpx.Client, admin_password: str, bot_password: str) -> None:
    """Phase 2: Log in as admin, then ensure bot user exists via admin API."""
    login_resp = client.post(
        f"{SYNAPSE_URL}/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": ADMIN_USERNAME},
            "password": admin_password,
        },
    )
    login_resp.raise_for_status()
    access_token = login_resp.json()["access_token"]
    print(f"Phase 2: Logged in as @{ADMIN_USERNAME}:{SERVER_NAME}")

    bot_mxid = f"@{BOT_USERNAME}:{SERVER_NAME}"
    encoded_mxid = urllib.parse.quote(bot_mxid)
    auth = {"Authorization": f"Bearer {access_token}"}
    url = _admin_user_url(encoded_mxid)

    if _bot_exists(client, access_token, encoded_mxid):
        # User exists — update displayname only. Do NOT include password:
        # Synapse invalidates all access tokens on password change, even if
        # the value is identical (bcrypt rehash triggers device purge).
        resp = client.put(url, json={"displayname": BOT_DISPLAYNAME, "admin": False}, headers=auth)
        resp.raise_for_status()
        print(f"Phase 2: Updated {bot_mxid} (displayname: {resp.json().get('displayname', 'n/a')})")
    else:
        resp = client.put(
            url, json={"password": bot_password, "displayname": BOT_DISPLAYNAME, "admin": False}, headers=auth
        )
        resp.raise_for_status()
        print(f"Phase 2: Created {bot_mxid} (displayname: {resp.json().get('displayname', 'n/a')})")


def main() -> None:
    registration_secret = os.environ["REGISTRATION_SECRET"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    bot_password = os.environ["BOT_PASSWORD"]

    with httpx.Client(timeout=30) as client:
        register_admin(client, registration_secret, admin_password)
        upsert_bot(client, admin_password, bot_password)
    print("Done: all Matrix users provisioned")


if __name__ == "__main__":
    main()
