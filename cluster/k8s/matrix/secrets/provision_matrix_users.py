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
import json
import os
import urllib.request

SYNAPSE_URL = "http://matrix-synapse.matrix.svc.cluster.local:8008"
ADMIN_USERNAME = "provisioner"
BOT_USERNAME = "openclaw"
BOT_DISPLAYNAME = "OpenClaw"
SERVER_NAME = "allegedly.works"


def _get_json(url: str, headers: dict) -> dict:
    hdrs = dict(headers)
    req = urllib.request.Request(url, headers=hdrs)
    return json.loads(urllib.request.urlopen(req).read())


def _post_json(url: str, data: dict, headers: dict | None = None) -> dict:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, json.dumps(data).encode(), headers=hdrs)
    return json.loads(urllib.request.urlopen(req).read())


def _put_json(url: str, data: dict, headers: dict) -> dict:
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers)
    req = urllib.request.Request(url, json.dumps(data).encode(), headers=hdrs, method="PUT")
    return json.loads(urllib.request.urlopen(req).read())


def register_admin(registration_secret: str, admin_password: str) -> None:
    """Phase 1: Register provisioner as admin via shared-secret."""
    # Get nonce
    req = urllib.request.Request(f"{SYNAPSE_URL}/_synapse/admin/v1/register")
    resp = json.loads(urllib.request.urlopen(req).read())
    nonce = resp["nonce"]

    # HMAC-SHA1: nonce\0username\0password\0admin
    mac_input = f"{nonce}\0{ADMIN_USERNAME}\0{admin_password}\0admin"
    mac = hmac.new(registration_secret.encode(), mac_input.encode(), hashlib.sha1).hexdigest()

    try:
        result = _post_json(
            f"{SYNAPSE_URL}/_synapse/admin/v1/register",
            {"nonce": nonce, "username": ADMIN_USERNAME, "password": admin_password, "admin": True, "mac": mac},
        )
        print(f"Phase 1: Registered admin @{result['user_id']}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 400 and "M_USER_IN_USE" in body:
            print(f"Phase 1: Admin @{ADMIN_USERNAME}:{SERVER_NAME} already exists, skipping")
            return
        raise RuntimeError(f"Phase 1 failed: HTTP {e.code} - {body}") from e


def _admin_user_url(encoded_mxid: str) -> str:
    return f"{SYNAPSE_URL}/_synapse/admin/v2/users/{encoded_mxid}"


def _bot_exists(access_token: str, encoded_mxid: str) -> bool:
    """Check if bot user already exists in Synapse."""
    try:
        user = _get_json(_admin_user_url(encoded_mxid), headers={"Authorization": f"Bearer {access_token}"})
        # Synapse returns the user object even for non-existent users but with
        # no "name" field. A real user always has "name".
        return "name" in user
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def upsert_bot(admin_password: str, bot_password: str) -> None:
    """Phase 2: Log in as admin, then ensure bot user exists via admin API."""
    login_resp = _post_json(
        f"{SYNAPSE_URL}/_matrix/client/v3/login",
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": ADMIN_USERNAME},
            "password": admin_password,
        },
    )
    access_token = login_resp["access_token"]
    print(f"Phase 2: Logged in as @{ADMIN_USERNAME}:{SERVER_NAME}")

    bot_mxid = f"@{BOT_USERNAME}:{SERVER_NAME}"
    encoded_mxid = urllib.request.quote(bot_mxid)
    auth = {"Authorization": f"Bearer {access_token}"}
    url = _admin_user_url(encoded_mxid)

    if _bot_exists(access_token, encoded_mxid):
        # User exists — update displayname only. Do NOT include password:
        # Synapse invalidates all access tokens on password change, even if
        # the value is identical (bcrypt rehash triggers device purge).
        result = _put_json(url, {"displayname": BOT_DISPLAYNAME, "admin": False}, headers=auth)
        print(f"Phase 2: Updated {bot_mxid} (displayname: {result.get('displayname', 'n/a')})")
    else:
        result = _put_json(
            url, {"password": bot_password, "displayname": BOT_DISPLAYNAME, "admin": False}, headers=auth
        )
        print(f"Phase 2: Created {bot_mxid} (displayname: {result.get('displayname', 'n/a')})")


def main() -> None:
    registration_secret = os.environ["REGISTRATION_SECRET"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    bot_password = os.environ["BOT_PASSWORD"]

    register_admin(registration_secret, admin_password)
    upsert_bot(admin_password, bot_password)
    print("Done: all Matrix users provisioned")


if __name__ == "__main__":
    main()
