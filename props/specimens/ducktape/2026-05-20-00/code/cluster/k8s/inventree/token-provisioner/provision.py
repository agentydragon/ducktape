"""Provision and auto-renew InvenTree API token for sandbox agents.

Creates a non-privileged sandbox-agent user in InvenTree (if absent), issues
a named API token for that user as superuser, and writes the token to a K8s
Secret in the inventree namespace. Reflector mirrors the Secret to
openclaw-sandbox and claude-sandbox.

On subsequent runs (CronJob), checks the token expiry via the InvenTree API
and renews it when fewer than RENEW_DAYS_BEFORE days remain: revokes the old
token, creates a fresh one, and patches the k8s Secret in-place.

InvenTree tokens expire after 365 days by default. The CronJob runs weekly;
with RENEW_DAYS_BEFORE=30 this gives a comfortable renewal window.

Requires: INVENTREE_ADMIN_USER, INVENTREE_ADMIN_PASSWORD env vars.
"""

import datetime
import os

from inventree.api import InvenTreeAPI
from inventree.user import User
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

INVENTREE_URL = "http://inventree:8000"
SECRET_NAME = "inventree-api-token"
INVENTREE_NS = "inventree"
SANDBOX_USERNAME = "sandbox-agent"
TOKEN_NAME = "inventree-token-provisioner"
RENEW_DAYS_BEFORE = 30

_SECRET_ANNOTATIONS = {
    "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
    "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "openclaw-sandbox,claude-sandbox",
    "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
    "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "openclaw-sandbox,claude-sandbox",
}


def get_or_create_sandbox_user(api: InvenTreeAPI) -> int:
    """Get or create the sandbox-agent user, returning their pk."""
    users = User.list(api)
    user = next((u for u in users if u["username"] == SANDBOX_USERNAME), None)

    if user is None:
        user = User.create(api, {"username": SANDBOX_USERNAME})
        print(f"Created user '{SANDBOX_USERNAME}' (pk={user.pk})")
    else:
        print(f"Found user '{SANDBOX_USERNAME}' (pk={user.pk})")

    return int(user.pk)


def find_token(api: InvenTreeAPI, user_pk: int) -> dict | None:
    """Return the named provisioner token for the sandbox user, or None."""
    tokens = api.get("user/tokens/", params={"all_users": True})
    if isinstance(tokens, dict):
        tokens = tokens.get("results", [])
    return next((t for t in (tokens or []) if t.get("user") == user_pk and t.get("name") == TOKEN_NAME), None)


def needs_renewal(token: dict) -> bool:
    """Return True if the token expires within RENEW_DAYS_BEFORE days."""
    expiry_str = token.get("expiry")
    if not expiry_str:
        print("Token has no expiry — skipping renewal.")
        return False
    expiry = datetime.date.fromisoformat(expiry_str)
    days_remaining = (expiry - datetime.date.today()).days
    print(f"Token '{TOKEN_NAME}' expires {expiry_str} ({days_remaining} days remaining).")
    return days_remaining < RENEW_DAYS_BEFORE


def provision_token(api: InvenTreeAPI, user_pk: int, existing: dict | None) -> str:
    """Revoke the existing token (if any) and create a fresh named one.

    The full token value is only present in the creation response, so we always
    create fresh rather than trying to retrieve an existing token's value.
    """
    if existing is not None:
        api.delete(f"user/tokens/{existing['pk']}/")
        print(f"Revoked token '{TOKEN_NAME}' (pk={existing['pk']})")

    response = api.post("user/tokens/", data={"user": user_pk, "name": TOKEN_NAME})
    token_key = response.get("token")
    if not token_key:
        raise RuntimeError(f"Token creation response missing 'token' field: {response}")
    print(f"Created token '{TOKEN_NAME}' for user pk={user_pk}")
    return str(token_key)


def main() -> None:
    admin_user = os.environ["INVENTREE_ADMIN_USER"]
    admin_password = os.environ["INVENTREE_ADMIN_PASSWORD"]

    config.load_incluster_config()
    v1 = client.CoreV1Api()

    api = InvenTreeAPI(INVENTREE_URL, username=admin_user, password=admin_password)
    user_pk = get_or_create_sandbox_user(api)
    existing_token = find_token(api, user_pk)

    try:
        v1.read_namespaced_secret(SECRET_NAME, INVENTREE_NS)
        secret_exists = True
    except ApiException as e:
        if e.status != 404:
            raise
        secret_exists = False

    if secret_exists:
        if existing_token is not None and not needs_renewal(existing_token):
            print("Token is fresh — done.")
            return
        if existing_token is None:
            print(f"Token '{TOKEN_NAME}' not found in InvenTree — reprovisioning.")

        token = provision_token(api, user_pk, existing=existing_token)
        print(f"Token renewed (first 8 chars): {token[:8]}...")
        v1.patch_namespaced_secret(
            SECRET_NAME,
            INVENTREE_NS,
            client.V1Secret(
                metadata=client.V1ObjectMeta(annotations=_SECRET_ANNOTATIONS),
                string_data={"token": token, "username": SANDBOX_USERNAME},
            ),
        )
        print(f"Secret {SECRET_NAME} updated in {INVENTREE_NS}.")
    else:
        token = provision_token(api, user_pk, existing=existing_token)
        print(f"Token obtained (first 8 chars): {token[:8]}...")
        v1.create_namespaced_secret(
            INVENTREE_NS,
            client.V1Secret(
                metadata=client.V1ObjectMeta(name=SECRET_NAME, namespace=INVENTREE_NS, annotations=_SECRET_ANNOTATIONS),
                string_data={"token": token, "username": SANDBOX_USERNAME},
            ),
        )
        print(f"Secret {SECRET_NAME} created in {INVENTREE_NS}.")

    print("Reflector will mirror to openclaw-sandbox and claude-sandbox.")


if __name__ == "__main__":
    main()
