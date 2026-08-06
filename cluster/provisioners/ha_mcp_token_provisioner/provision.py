"""Provision and repair the Home Assistant token consumed by HA-MCP."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import websockets
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

DEFAULT_HA_URL = "http://home-assistant.home-assistant.svc.cluster.local:8123"
CLIENT_ID = "https://home.allegedly.works/"
USERNAME = "ha-local-admin"
TOKEN_SECRET_NAME = "ha-mcp-home-assistant-token"
TOKEN_SECRET_NAMESPACE = "ha-mcp"
CLIENT_NAME = "ha-mcp-cluster"
LIFESPAN_DAYS = 3650
# `type` in an auth/refresh_tokens entry; homeassistant.auth.models.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN.
LONG_LIVED_TOKEN_TYPE = "long_lived_access_token"


def required_string(value: object, *path: str) -> str:
    """Read a required string from a nested JSON object."""
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Response is missing {'.'.join(path)}")
        value = value[key]
    if not isinstance(value, str):
        raise TypeError(f"Response field {'.'.join(path)} is not a string")
    return value


def token_is_valid(http: httpx.Client, token: str) -> bool:
    """Return whether Home Assistant currently accepts the token."""
    response = http.get("/api/", headers={"Authorization": f"Bearer {token}"})
    if response.status_code in (401, 403):
        return False
    response.raise_for_status()
    return True


def login(http: httpx.Client, password: str) -> str:
    """Log in as the local owner and return a short-lived access token."""
    response = http.post(
        "/auth/login_flow", json={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID}
    )
    response.raise_for_status()
    flow_id = required_string(response.json(), "flow_id")

    response = http.post(
        f"/auth/login_flow/{flow_id}", json={"client_id": CLIENT_ID, "username": USERNAME, "password": password}
    )
    response.raise_for_status()
    auth_code = required_string(response.json(), "result")

    response = http.post(
        "/auth/token", data={"grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT_ID}
    )
    response.raise_for_status()
    return required_string(response.json(), "access_token")


class Session:
    """Authenticated Home Assistant websocket connection that correlates replies by message id."""

    def __init__(self, websocket: Any):
        self._websocket = websocket
        self._last_id = 0

    async def command(self, command_type: str, **payload: Any) -> Any:
        self._last_id += 1
        message_id = self._last_id
        await self._websocket.send(json.dumps({"id": message_id, "type": command_type, **payload}))
        while True:
            response = json.loads(await self._websocket.recv())
            # This connection subscribes to nothing, so anything else is an unsolicited
            # event; skipping by id keeps one from being mistaken for our reply.
            if response.get("id") != message_id:
                continue
            if response.get("success") is not True:
                raise RuntimeError(f"Home Assistant rejected {command_type}: {response.get('error')}")
            return response.get("result")


async def existing_token_ids(session: Session) -> list[str]:
    """Return the ids of the long-lived tokens this provisioner already owns."""
    tokens = await session.command("auth/refresh_tokens")
    # The session's own refresh token came from the authorization-code grant in login(),
    # so its type is `normal` and it can never match here -- revoking these cannot cut
    # the connection doing the revoking.
    return [
        token["id"]
        for token in tokens
        if token.get("type") == LONG_LIVED_TOKEN_TYPE and token.get("client_name") == CLIENT_NAME
    ]


async def replace_long_lived_token(access_token: str, websocket_url: str) -> str:
    """Revoke any long-lived token this provisioner owns, then mint a replacement.

    Revoking first is required, not tidiness: Home Assistant rejects a second long-lived
    token whose `client_name` already exists (`async_create_refresh_token` raises
    `ValueError`), and the websocket API reports that as a bare `unknown_error`. Minting
    without revoking therefore deadlocks the moment the Secret's token stops validating
    while the Home Assistant side still holds one -- every run fails identically forever.
    """
    async with websockets.connect(websocket_url) as websocket:
        greeting = json.loads(await websocket.recv())
        if greeting.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected websocket greeting: {greeting.get('type')}")

        await websocket.send(json.dumps({"type": "auth", "access_token": access_token}))
        auth_result = json.loads(await websocket.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant websocket auth failed: {auth_result.get('type')}")

        session = Session(websocket)
        for token_id in await existing_token_ids(session):
            await session.command("auth/delete_refresh_token", refresh_token_id=token_id)
            print(f"Revoked stale {CLIENT_NAME} long-lived token {token_id}")

        result = await session.command("auth/long_lived_access_token", client_name=CLIENT_NAME, lifespan=LIFESPAN_DAYS)
        if not isinstance(result, str):
            raise TypeError(f"Home Assistant returned a non-string token: {type(result).__name__}")
        return result


def read_token_secret(v1: Any) -> tuple[bool, str | None]:
    """Read the managed Secret and return whether it exists and its decoded token."""
    try:
        secret = v1.read_namespaced_secret(TOKEN_SECRET_NAME, TOKEN_SECRET_NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            return False, None
        raise

    encoded = (secret.data or {}).get("token")
    if not encoded:
        return True, None
    return True, base64.b64decode(encoded).decode()


def write_token_secret(v1: Any, *, exists: bool, token: str) -> None:
    """Create or patch the narrowly managed Kubernetes Secret without logging its value."""
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=TOKEN_SECRET_NAME,
            namespace=TOKEN_SECRET_NAMESPACE,
            annotations={"description": "Automatically provisioned Home Assistant long-lived token for HA-MCP"},
        ),
        string_data={"token": token},
        type="Opaque",
    )
    if exists:
        v1.patch_namespaced_secret(TOKEN_SECRET_NAME, TOKEN_SECRET_NAMESPACE, secret)
    else:
        v1.create_namespaced_secret(TOKEN_SECRET_NAMESPACE, secret)


def provision(v1: Any, http: httpx.Client, password: str, replace_token: Callable[[str], str]) -> bool:
    """Ensure a valid token exists; return whether the Secret was changed."""
    exists, token = read_token_secret(v1)
    if token is not None and token_is_valid(http, token):
        print("HA-MCP Home Assistant token is valid")
        return False

    access_token = login(http, password)
    write_token_secret(v1, exists=exists, token=replace_token(access_token))
    print("Provisioned a valid HA-MCP Home Assistant token")
    return True


def main() -> None:
    """Provision the cluster token using in-cluster Kubernetes credentials."""
    ha_url = os.environ.get("HOME_ASSISTANT_URL", DEFAULT_HA_URL).rstrip("/")
    websocket_url = ha_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
    password = os.environ["HOME_ASSISTANT_LOCAL_ADMIN_PASSWORD"]

    config.load_incluster_config()
    v1 = client.CoreV1Api()
    with httpx.Client(base_url=ha_url, timeout=30) as http:
        provision(
            v1, http, password, lambda access_token: asyncio.run(replace_long_lived_token(access_token, websocket_url))
        )


if __name__ == "__main__":
    main()
