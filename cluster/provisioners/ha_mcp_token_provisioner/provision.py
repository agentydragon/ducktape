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


async def mint_long_lived_token(access_token: str, websocket_url: str) -> str:
    """Mint the long-lived token consumed by HA-MCP over Home Assistant's websocket API."""
    async with websockets.connect(websocket_url) as websocket:
        greeting = json.loads(await websocket.recv())
        if greeting.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected websocket greeting: {greeting.get('type')}")

        await websocket.send(json.dumps({"type": "auth", "access_token": access_token}))
        auth_result = json.loads(await websocket.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant websocket auth failed: {auth_result.get('type')}")

        await websocket.send(
            json.dumps(
                {"id": 1, "type": "auth/long_lived_access_token", "client_name": "ha-mcp-cluster", "lifespan": 3650}
            )
        )
        response = json.loads(await websocket.recv())
        if response.get("success") is not True:
            raise RuntimeError(f"Home Assistant rejected token creation: {response.get('error')}")
        return required_string(response, "result")


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


def provision(v1: Any, http: httpx.Client, password: str, mint_token: Callable[[str], str]) -> bool:
    """Ensure a valid token exists; return whether the Secret was changed."""
    exists, token = read_token_secret(v1)
    if token is not None and token_is_valid(http, token):
        print("HA-MCP Home Assistant token is valid")
        return False

    access_token = login(http, password)
    write_token_secret(v1, exists=exists, token=mint_token(access_token))
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
            v1, http, password, lambda access_token: asyncio.run(mint_long_lived_token(access_token, websocket_url))
        )


if __name__ == "__main__":
    main()
