"""Keep each configured Secret holding a long-lived token of the local owner that Home Assistant
accepts, minting a replacement when it holds none or one Home Assistant refuses."""

from __future__ import annotations

import base64

import kubernetes
from client import HomeAssistantClient
from settings import TokenConfig

LIFESPAN_DAYS = 3650
# `type` in an auth/refresh_tokens entry; homeassistant.auth.models.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN.
LONG_LIVED_TOKEN_TYPE = "long_lived_access_token"


async def replace_long_lived_token(home_assistant: HomeAssistantClient, client_name: str) -> str:
    """Revoke the logged-in user's long-lived tokens named `client_name`, then mint a replacement.

    Revoking first is required: Home Assistant rejects a second long-lived token whose
    `client_name` already exists (`async_create_refresh_token` raises `ValueError`), and the
    websocket API reports that as a bare `unknown_error`. Minting without revoking therefore
    deadlocks once the Secret's token stops validating while Home Assistant still holds one.
    """
    tokens = await home_assistant.websocket_command({"id": 1, "type": "auth/refresh_tokens"})
    if not isinstance(tokens, list):
        raise TypeError(f"Home Assistant returned an invalid token list: {tokens!r}")
    # The logged-in session's own refresh token came from an authorization-code grant, so its type
    # is `normal` and never matches here -- revoking these cannot cut the session doing it.
    for token in tokens:
        if token.get("type") == LONG_LIVED_TOKEN_TYPE and token.get("client_name") == client_name:
            await home_assistant.websocket_command(
                {"id": 1, "type": "auth/delete_refresh_token", "refresh_token_id": token["id"]}
            )
            print(f"Revoked stale {client_name} long-lived token {token['id']}")
    minted = await home_assistant.websocket_command(
        {"id": 1, "type": "auth/long_lived_access_token", "client_name": client_name, "lifespan": LIFESPAN_DAYS}
    )
    if not isinstance(minted, str):
        raise TypeError(f"Home Assistant returned a non-string token: {type(minted).__name__}")
    return minted


def read_token_secret(v1: kubernetes.client.CoreV1Api, token: TokenConfig) -> tuple[bool, str | None]:
    """Whether the token's Secret exists, and the token it holds."""
    try:
        secret = v1.read_namespaced_secret(token.secret_name, token.secret_namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            return False, None
        raise
    encoded = (secret.data or {}).get("token")
    if not encoded:
        return True, None
    return True, base64.b64decode(encoded).decode()


def write_token_secret(v1: kubernetes.client.CoreV1Api, token: TokenConfig, *, exists: bool, value: str) -> None:
    """Create or patch the token's Secret."""
    secret = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(
            name=token.secret_name, namespace=token.secret_namespace, annotations={"description": token.description}
        ),
        string_data={"token": value},
        type="Opaque",
    )
    if exists:
        v1.patch_namespaced_secret(token.secret_name, token.secret_namespace, secret)
    else:
        v1.create_namespaced_secret(token.secret_namespace, secret)


async def provision_token(
    home_assistant: HomeAssistantClient, v1: kubernetes.client.CoreV1Api, token: TokenConfig, password: str
) -> bool:
    """Leave a Secret whose token Home Assistant accepts, else mint one into it; return whether it
    changed."""
    exists, current = read_token_secret(v1, token)
    if current is not None and await home_assistant.token_is_valid(current):
        print(f"{token.secret_name} holds a valid token")
        return False
    await home_assistant.login(password)
    write_token_secret(
        v1, token, exists=exists, value=await replace_long_lived_token(home_assistant, token.client_name)
    )
    print(f"Provisioned a valid token into {token.secret_name}")
    return True
