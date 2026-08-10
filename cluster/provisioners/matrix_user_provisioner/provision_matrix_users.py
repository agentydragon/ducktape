"""Provision Matrix users on Synapse: admin + the Haku bot, and Haku's token.

Three-phase idempotent provisioning:
  Phase 1: Register provisioner as admin via shared-secret endpoint.
           Skips if user already exists (M_USER_IN_USE).
  Phase 2: Log in as provisioner, then ensure the bot user exists via Synapse
           admin API. Only sets password on initial creation — setting password
           on an existing user invalidates all access tokens (Synapse re-hashes
           with bcrypt and deletes all devices/tokens), which breaks the bot's
           active Matrix session.
  Phase 3: Ensure the Secret holding Haku's access token contains a token that
           still works, minting a fresh one through the admin login-as-user
           endpoint when it does not. haku-console reads that Secret to sync as
           the bot; see haku/plans/matrix_chat_runtime.md R10.3.

Requires: REGISTRATION_SECRET, ADMIN_PASSWORD, BOT_PASSWORD env vars.
"""

import base64
import hashlib
import hmac
import logging
import os
import urllib.parse
from typing import Protocol

import httpx
from kubernetes import client as k8s, config as k8s_config
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

SYNAPSE_URL = "http://matrix-synapse.matrix.svc.cluster.local:8008"
ADMIN_USERNAME = "provisioner"
BOT_USERNAME = "haku"
BOT_DISPLAYNAME = "Haku"
SERVER_NAME = "allegedly.works"

MATRIX_NS = "matrix"
TOKEN_SECRET_NAME = "haku-matrix-token"
TOKEN_SECRET_KEY = "access_token"


class SynapseClient(Protocol):
    """The subset of httpx.Client's interface this module needs — lets tests
    inject a lightweight fake without subclassing httpx.Client."""

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response: ...
    def post(self, url: str, *, json: dict | None = None, headers: dict[str, str] | None = None) -> httpx.Response: ...
    def put(self, url: str, *, json: dict | None = None, headers: dict[str, str] | None = None) -> httpx.Response: ...


class SecretStore(Protocol):
    """The subset of CoreV1Api this module needs, for the same reason as
    SynapseClient above: tests inject a fake rather than a real API client."""

    def read_namespaced_secret(self, name: str, namespace: str) -> k8s.V1Secret: ...
    def patch_namespaced_secret(self, name: str, namespace: str, body: k8s.V1Secret) -> k8s.V1Secret: ...
    def create_namespaced_secret(self, namespace: str, body: k8s.V1Secret) -> k8s.V1Secret: ...


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


def upsert_bot(client: SynapseClient, admin_token: str, bot_password: str) -> None:
    """Phase 2: Ensure the bot user exists via the admin API."""
    bot_mxid = f"@{BOT_USERNAME}:{SERVER_NAME}"
    encoded_mxid = urllib.parse.quote(bot_mxid)
    auth = {"Authorization": f"Bearer {admin_token}"}
    url = _admin_user_url(encoded_mxid)

    if _bot_exists(client, admin_token, encoded_mxid):
        # User exists — update displayname only. Do NOT include password:
        # Synapse invalidates all access tokens on password change, even if
        # the value is identical (bcrypt rehash triggers device purge).
        resp = client.put(url, json={"displayname": BOT_DISPLAYNAME, "admin": False}, headers=auth)
        resp.raise_for_status()
        logger.info("Phase 2: Updated %s (displayname: %s)", bot_mxid, resp.json().get("displayname", "n/a"))
    else:
        resp = client.put(
            url, json={"password": bot_password, "displayname": BOT_DISPLAYNAME, "admin": False}, headers=auth
        )
        resp.raise_for_status()
        logger.info("Phase 2: Created %s (displayname: %s)", bot_mxid, resp.json().get("displayname", "n/a"))


def admin_login(client: SynapseClient, admin_password: str) -> str:
    """Log in as the provisioner admin and return its access token."""
    resp = client.post(
        f"{SYNAPSE_URL}/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": ADMIN_USERNAME},
            "password": admin_password,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def token_is_valid(client: SynapseClient, token: str) -> bool:
    """True if `token` still authenticates as the bot.

    Synapse invalidates a user's tokens whenever their password is set, so a
    stored token can stop working without anything here changing.
    """
    resp = client.get(f"{SYNAPSE_URL}/_matrix/client/v3/account/whoami", headers={"Authorization": f"Bearer {token}"})
    return resp.status_code == 200 and resp.json().get("user_id") == f"@{BOT_USERNAME}:{SERVER_NAME}"


def mint_bot_token(client: SynapseClient, admin_token: str) -> str:
    """Mint a non-expiring access token for the bot via admin login-as-user.

    Preferred over logging in with the bot's password: it leaves the password
    untouched, and so leaves any other live session intact.
    """
    encoded_mxid = urllib.parse.quote(f"@{BOT_USERNAME}:{SERVER_NAME}")
    resp = client.post(
        f"{SYNAPSE_URL}/_synapse/admin/v1/users/{encoded_mxid}/login",
        json={},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def _core_v1() -> k8s.CoreV1Api:
    k8s_config.load_incluster_config()
    return k8s.CoreV1Api()


def read_stored_token(v1: SecretStore) -> str | None:
    """The token currently in the Secret, or None if absent."""
    try:
        secret = v1.read_namespaced_secret(TOKEN_SECRET_NAME, MATRIX_NS)
    except ApiException as error:
        if error.status == 404:
            return None
        raise
    encoded = (secret.data or {}).get(TOKEN_SECRET_KEY)
    return None if encoded is None else base64.b64decode(encoded).decode()


def write_token(v1: SecretStore, token: str) -> None:
    """Create or patch the Secret holding the bot's access token."""
    body = k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(name=TOKEN_SECRET_NAME, namespace=MATRIX_NS),
        string_data={TOKEN_SECRET_KEY: token, "user_id": f"@{BOT_USERNAME}:{SERVER_NAME}"},
    )
    try:
        v1.patch_namespaced_secret(TOKEN_SECRET_NAME, MATRIX_NS, body)
        logger.info("Phase 3: Patched Secret %s/%s", MATRIX_NS, TOKEN_SECRET_NAME)
    except ApiException as error:
        if error.status != 404:
            raise
        v1.create_namespaced_secret(MATRIX_NS, body)
        logger.info("Phase 3: Created Secret %s/%s", MATRIX_NS, TOKEN_SECRET_NAME)


def ensure_bot_token(client: SynapseClient, v1: SecretStore, admin_token: str) -> None:
    """Phase 3: Keep a working bot access token in the Secret."""
    stored = read_stored_token(v1)
    if stored is not None and token_is_valid(client, stored):
        logger.info("Phase 3: Stored token still valid, leaving it alone")
        return
    write_token(v1, mint_bot_token(client, admin_token))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    registration_secret = os.environ["REGISTRATION_SECRET"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    bot_password = os.environ["BOT_PASSWORD"]

    with httpx.Client(timeout=30) as client:
        register_admin(client, registration_secret, admin_password)
        admin_token = admin_login(client, admin_password)
        logger.info("Logged in as @%s:%s", ADMIN_USERNAME, SERVER_NAME)
        upsert_bot(client, admin_token, bot_password)
        ensure_bot_token(client, _core_v1(), admin_token)
    logger.info("Done: Matrix users provisioned and Haku's token in place")


if __name__ == "__main__":
    main()
