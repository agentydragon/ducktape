"""Complete Home Assistant onboarding with a local break-glass owner."""

from __future__ import annotations

import asyncio
import json
import os
import time
from http import HTTPStatus
from urllib import error, parse, request

import aiohttp

BASE_URL = os.environ.get("HOME_ASSISTANT_URL", "http://home-assistant.home-assistant.svc.cluster.local:8123")
CLIENT_ID = "https://home.allegedly.works/"
REDIRECT_URI = CLIENT_ID
USERNAME = "ha-local-admin"
DISPLAY_NAME = "Home Assistant Local Administrator"
REQUIRED_STEPS = frozenset({"user", "core_config", "integration", "analytics"})
HTTP_CONFIG = {
    "server_host": ["127.0.0.1"],
    "server_port": 8124,
    "cors_allowed_origins": ["https://cast.home-assistant.io"],
    "use_x_forwarded_for": True,
    "trusted_proxies": ["127.0.0.1/32"],
    "login_attempts_threshold": -1,
    "ip_ban_enabled": True,
    "ssl_profile": "modern",
    "use_x_frame_options": True,
}
HTTP_CONFIG_METADATA = frozenset({"created_at", "error", "error_message"})


def request_json(
    path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
) -> object:
    """Send a request to Home Assistant and decode its JSON response."""
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with request.urlopen(request.Request(f"{BASE_URL}{path}", data=body, headers=headers), timeout=30) as response:
        return json.load(response)


def wait_for_home_assistant() -> set[str] | None:
    """Wait for the API and return onboarding state, or None when complete."""
    for _ in range(60):
        try:
            verify_api_ready()
            return onboarding_status()
        except error.URLError, TimeoutError:
            time.sleep(5)
    raise TimeoutError("Home Assistant did not become available within 5 minutes")


def verify_api_ready() -> None:
    """Require Home Assistant's unauthenticated API response."""
    try:
        request_json("/api/")
    except error.HTTPError as exc:
        if exc.code == HTTPStatus.UNAUTHORIZED:
            return
        raise
    raise RuntimeError("Home Assistant API unexpectedly allowed an unauthenticated request")


def onboarding_status() -> set[str] | None:
    """Return completed steps, or None when onboarding views are absent."""
    try:
        response = request_json("/api/onboarding")
    except error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            return None
        raise
    if not isinstance(response, list):
        raise TypeError("Home Assistant returned an invalid onboarding status")
    return {
        step["step"]
        for step in response
        if isinstance(step, dict) and step.get("done") is True and isinstance(step.get("step"), str)
    }


def required_string(response: object, *path: str) -> str:
    """Read a required string from a JSON object."""
    value = response
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Home Assistant response is missing {'.'.join(path)}")
        value = value[key]
    if not isinstance(value, str):
        raise TypeError(f"Home Assistant response field {'.'.join(path)} is not a string")
    return value


def create_owner(password: str) -> str:
    """Create the local owner and return an authorization code."""
    return required_string(
        request_json(
            "/api/onboarding/users",
            data={
                "name": DISPLAY_NAME,
                "username": USERNAME,
                "password": password,
                "client_id": CLIENT_ID,
                "language": "en",
            },
        ),
        "auth_code",
    )


def login(password: str) -> str:
    """Authenticate the local owner after a partially completed run."""
    flow_id = required_string(
        request_json(
            "/auth/login_flow",
            data={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": REDIRECT_URI},
        ),
        "flow_id",
    )
    return required_string(
        request_json(
            f"/auth/login_flow/{flow_id}", data={"client_id": CLIENT_ID, "username": USERNAME, "password": password}
        ),
        "result",
    )


def exchange_token(auth_code: str) -> str:
    """Exchange a Home Assistant authorization code for an access token."""
    return required_string(
        request_json(
            "/auth/token",
            data={"grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT_ID},
            form=True,
        ),
        "access_token",
    )


def websocket_url() -> str:
    """Return the Home Assistant WebSocket API URL."""
    parsed = parse.urlsplit(BASE_URL)
    websocket_scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
    if websocket_scheme is None:
        raise ValueError(f"Unsupported Home Assistant URL scheme: {parsed.scheme}")
    return parse.urlunsplit((websocket_scheme, parsed.netloc, "/api/websocket", "", ""))


async def websocket_command(token: str, message: dict[str, object]) -> object:
    """Authenticate to Home Assistant and execute one WebSocket command."""
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(websocket_url(), timeout=aiohttp.ClientWSTimeout(ws_receive=30)) as websocket,
    ):
        auth_required = await websocket.receive_json()
        if not isinstance(auth_required, dict) or auth_required.get("type") != "auth_required":
            raise RuntimeError(f"Home Assistant WebSocket did not request authentication: {auth_required!r}")
        await websocket.send_json({"type": "auth", "access_token": token})
        auth_result = await websocket.receive_json()
        if not isinstance(auth_result, dict) or auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant WebSocket authentication failed: {auth_result!r}")
        await websocket.send_json(message)
        result = await websocket.receive_json()
    if not isinstance(result, dict) or result.get("type") != "result" or result.get("success") is not True:
        raise RuntimeError(f"Home Assistant WebSocket command failed: {result!r}")
    return result.get("result")


def config_without_metadata(config: object) -> dict[str, object]:
    """Validate and remove runtime metadata from a stored HTTP config."""
    if not isinstance(config, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config: {config!r}")
    return {key: value for key, value in config.items() if key not in HTTP_CONFIG_METADATA}


async def configure_http(password: str, token: str) -> None:
    """Converge Home Assistant's UI-managed HTTP settings through its admin API."""
    current = await websocket_command(token, {"id": 1, "type": "http/config"})
    if not isinstance(current, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config response: {current!r}")
    stable = config_without_metadata(current.get("stable"))
    pending = current.get("pending")
    pending_config = config_without_metadata(pending) if pending is not None else None
    if stable == HTTP_CONFIG and pending is None:
        return
    if pending_config == HTTP_CONFIG and current.get("active_config_type") == "pending":
        await websocket_command(token, {"id": 1, "type": "http/config/promote"})
        return

    result = await websocket_command(token, {"id": 1, "type": "http/config/configure", "config": HTTP_CONFIG})
    if not isinstance(result, dict) or not isinstance(result.get("restart"), bool):
        raise TypeError(f"Home Assistant returned an invalid HTTP configure response: {result!r}")
    if not result["restart"]:
        return

    await asyncio.to_thread(wait_for_home_assistant)
    refreshed_token = await asyncio.to_thread(exchange_token, await asyncio.to_thread(login, password))
    await websocket_command(refreshed_token, {"id": 1, "type": "http/config/promote"})


async def provision(password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    completed = await asyncio.to_thread(wait_for_home_assistant)
    if completed is None or completed >= REQUIRED_STEPS:
        token = await asyncio.to_thread(exchange_token, await asyncio.to_thread(login, password))
        await configure_http(password, token)
        print("Home Assistant onboarding is already complete")
        return

    auth_code = (
        await asyncio.to_thread(login, password)
        if "user" in completed
        else await asyncio.to_thread(create_owner, password)
    )
    token = await asyncio.to_thread(exchange_token, auth_code)
    if "core_config" not in completed:
        await asyncio.to_thread(request_json, "/api/onboarding/core_config", data={}, token=token)
    if "integration" not in completed:
        await asyncio.to_thread(
            request_json,
            "/api/onboarding/integration",
            data={"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI},
            token=token,
        )
    if "analytics" not in completed:
        await asyncio.to_thread(request_json, "/api/onboarding/analytics", data={}, token=token)
    await configure_http(password, token)
    print("Home Assistant onboarding is complete")


async def main() -> None:
    await provision(os.environ["HOME_ASSISTANT_LOCAL_ADMIN_PASSWORD"])


if __name__ == "__main__":
    asyncio.run(main())
