"""Complete Home Assistant onboarding with a local break-glass owner."""

from __future__ import annotations

import asyncio
import json
import os
import time
from http import HTTPStatus
from pathlib import Path
from urllib import error, parse, request

import aiohttp
from component_installer import install_components
from settings import ProvisionerSettings, load_settings


def request_json(
    settings: ProvisionerSettings,
    path: str,
    *,
    data: dict[str, object] | None = None,
    token: str | None = None,
    form: bool = False,
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
    with request.urlopen(
        request.Request(f"{settings.home_assistant_url}{path}", data=body, headers=headers), timeout=30
    ) as response:
        return json.load(response)


def wait_for_home_assistant(settings: ProvisionerSettings) -> set[str] | None:
    """Wait for the API and return onboarding state, or None when complete."""
    for _ in range(60):
        try:
            verify_api_ready(settings)
            return onboarding_status(settings)
        except error.URLError, TimeoutError:
            time.sleep(5)
    raise TimeoutError("Home Assistant did not become available within 5 minutes")


def verify_api_ready(settings: ProvisionerSettings) -> None:
    """Require Home Assistant's unauthenticated API response."""
    try:
        request_json(settings, "/api/")
    except error.HTTPError as exc:
        if exc.code == HTTPStatus.UNAUTHORIZED:
            return
        raise
    raise RuntimeError("Home Assistant API unexpectedly allowed an unauthenticated request")


def onboarding_status(settings: ProvisionerSettings) -> set[str] | None:
    """Return completed steps, or None when onboarding views are absent."""
    try:
        response = request_json(settings, "/api/onboarding")
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


def create_owner(settings: ProvisionerSettings, password: str) -> str:
    """Create the local owner and return an authorization code."""
    return required_string(
        request_json(
            settings,
            "/api/onboarding/users",
            data={
                "name": settings.display_name,
                "username": settings.username,
                "password": password,
                "client_id": settings.client_id,
                "language": "en",
            },
        ),
        "auth_code",
    )


def login(settings: ProvisionerSettings, password: str) -> str:
    """Authenticate the local owner after a partially completed run."""
    flow_id = required_string(
        request_json(
            settings,
            "/auth/login_flow",
            data={
                "client_id": settings.client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": settings.redirect_uri,
            },
        ),
        "flow_id",
    )
    return required_string(
        request_json(
            settings,
            f"/auth/login_flow/{flow_id}",
            data={"client_id": settings.client_id, "username": settings.username, "password": password},
        ),
        "result",
    )


def exchange_token(settings: ProvisionerSettings, auth_code: str) -> str:
    """Exchange a Home Assistant authorization code for an access token."""
    return required_string(
        request_json(
            settings,
            "/auth/token",
            data={"grant_type": "authorization_code", "code": auth_code, "client_id": settings.client_id},
            form=True,
        ),
        "access_token",
    )


def websocket_url(settings: ProvisionerSettings) -> str:
    """Return the Home Assistant WebSocket API URL."""
    parsed = parse.urlsplit(settings.home_assistant_url)
    websocket_scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
    if websocket_scheme is None:
        raise ValueError(f"Unsupported Home Assistant URL scheme: {parsed.scheme}")
    return parse.urlunsplit((websocket_scheme, parsed.netloc, "/api/websocket", "", ""))


async def websocket_command(settings: ProvisionerSettings, token: str, message: dict[str, object]) -> object:
    """Authenticate to Home Assistant and execute one WebSocket command."""
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(websocket_url(settings), timeout=aiohttp.ClientWSTimeout(ws_receive=30)) as websocket,
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
    return {key: value for key, value in config.items() if key not in {"created_at", "error", "error_message"}}


async def configure_http(settings: ProvisionerSettings, password: str, token: str) -> None:
    """Converge Home Assistant's UI-managed HTTP settings through its admin API."""
    http_config = settings.http_config.model_dump()
    current = await websocket_command(settings, token, {"id": 1, "type": "http/config"})
    if not isinstance(current, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config response: {current!r}")
    stable = config_without_metadata(current.get("stable"))
    pending = current.get("pending")
    pending_config = config_without_metadata(pending) if pending is not None else None
    if stable == http_config and pending is None:
        return
    if pending_config == http_config and current.get("active_config_type") == "pending":
        await websocket_command(settings, token, {"id": 1, "type": "http/config/promote"})
        return

    result = await websocket_command(settings, token, {"id": 1, "type": "http/config/configure", "config": http_config})
    if not isinstance(result, dict) or not isinstance(result.get("restart"), bool):
        raise TypeError(f"Home Assistant returned an invalid HTTP configure response: {result!r}")
    if not result["restart"]:
        return

    await asyncio.to_thread(wait_for_home_assistant, settings)
    refreshed_token = await asyncio.to_thread(
        exchange_token, settings, await asyncio.to_thread(login, settings, password)
    )
    await websocket_command(settings, refreshed_token, {"id": 1, "type": "http/config/promote"})


async def provision(settings: ProvisionerSettings, password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    completed = await asyncio.to_thread(wait_for_home_assistant, settings)
    required_steps = settings.required_onboarding_steps
    if completed is None or completed >= required_steps:
        token = await asyncio.to_thread(exchange_token, settings, await asyncio.to_thread(login, settings, password))
        await configure_http(settings, password, token)
        print("Home Assistant onboarding is already complete")
        return

    auth_code = (
        await asyncio.to_thread(login, settings, password)
        if "user" in completed
        else await asyncio.to_thread(create_owner, settings, password)
    )
    token = await asyncio.to_thread(exchange_token, settings, auth_code)
    if "core_config" not in completed:
        await asyncio.to_thread(request_json, settings, "/api/onboarding/core_config", data={}, token=token)
    if "integration" not in completed:
        await asyncio.to_thread(
            request_json,
            settings,
            "/api/onboarding/integration",
            data={"client_id": settings.client_id, "redirect_uri": settings.redirect_uri},
            token=token,
        )
    if "analytics" not in completed:
        await asyncio.to_thread(request_json, settings, "/api/onboarding/analytics", data={}, token=token)
    await configure_http(settings, password, token)
    print("Home Assistant onboarding is complete")


def main() -> None:
    """Install configured components and complete configured onboarding."""
    settings = load_settings()
    config_dir = Path("/config")
    install_components(config_dir, settings.components)
    if settings.onboarding_enabled:
        asyncio.run(provision(settings, os.environ["HOME_ASSISTANT_LOCAL_ADMIN_PASSWORD"]))


if __name__ == "__main__":
    main()
