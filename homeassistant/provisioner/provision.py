"""Complete Home Assistant onboarding with a local break-glass owner."""

from __future__ import annotations

import asyncio
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from urllib import error, parse

import aiohttp
from component_installer import install_components
from settings import ProvisionerSettings, load_settings


async def request_json(
    session: aiohttp.ClientSession,
    settings: ProvisionerSettings,
    path: str,
    *,
    data: dict[str, object] | None = None,
    token: str | None = None,
    form: bool = False,
) -> object:
    """Send a request to Home Assistant and decode its JSON response."""
    headers = {"Accept": "application/json"}
    method = "GET" if data is None else "POST"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{settings.home_assistant_url}{path}"
    timeout = aiohttp.ClientTimeout(total=30)
    if data is None:
        request_context = session.request(method, url, headers=headers, timeout=timeout)
    elif form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_context = session.request(
            method, url, headers=headers, timeout=timeout, data=parse.urlencode(data).encode()
        )
    else:
        request_context = session.request(method, url, headers=headers, timeout=timeout, json=data)
    async with request_context as response:
        if response.status >= 400:
            raise error.HTTPError(str(response.url), response.status, response.reason or "", Message(), None)
        return await response.json()


async def wait_for_home_assistant(session: aiohttp.ClientSession, settings: ProvisionerSettings) -> set[str] | None:
    """Wait for the API and return onboarding state, or None when complete."""
    for attempt in range(60):
        try:
            await verify_api_ready(session, settings)
            return await onboarding_status(session, settings)
        except aiohttp.ClientError, error.URLError, TimeoutError:
            if attempt < 59:
                await asyncio.sleep(5)
    raise TimeoutError("Home Assistant did not become available within 5 minutes")


async def verify_api_ready(session: aiohttp.ClientSession, settings: ProvisionerSettings) -> None:
    """Require Home Assistant's unauthenticated API response."""
    try:
        await request_json(session, settings, "/api/")
    except error.HTTPError as exc:
        if exc.code == HTTPStatus.UNAUTHORIZED:
            return
        raise
    raise RuntimeError("Home Assistant API unexpectedly allowed an unauthenticated request")


async def onboarding_status(session: aiohttp.ClientSession, settings: ProvisionerSettings) -> set[str] | None:
    """Return completed steps, or None when onboarding views are absent."""
    try:
        response = await request_json(session, settings, "/api/onboarding")
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


async def create_owner(session: aiohttp.ClientSession, settings: ProvisionerSettings, password: str) -> str:
    """Create the local owner and return an authorization code."""
    return required_string(
        await request_json(
            session,
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


async def login(session: aiohttp.ClientSession, settings: ProvisionerSettings, password: str) -> str:
    """Authenticate the local owner after a partially completed run."""
    flow_id = required_string(
        await request_json(
            session,
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
        await request_json(
            session,
            settings,
            f"/auth/login_flow/{flow_id}",
            data={"client_id": settings.client_id, "username": settings.username, "password": password},
        ),
        "result",
    )


async def exchange_token(session: aiohttp.ClientSession, settings: ProvisionerSettings, auth_code: str) -> str:
    """Exchange a Home Assistant authorization code for an access token."""
    return required_string(
        await request_json(
            session,
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


async def websocket_command(
    session: aiohttp.ClientSession, settings: ProvisionerSettings, token: str, message: dict[str, object]
) -> object:
    """Authenticate to Home Assistant and execute one WebSocket command."""
    async with session.ws_connect(websocket_url(settings), timeout=aiohttp.ClientWSTimeout(ws_receive=30)) as websocket:
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


async def configure_http(
    session: aiohttp.ClientSession, settings: ProvisionerSettings, password: str, token: str
) -> None:
    """Converge Home Assistant's UI-managed HTTP settings through its admin API."""
    http_config = settings.http_config.model_dump()
    current = await websocket_command(session, settings, token, {"id": 1, "type": "http/config"})
    if not isinstance(current, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config response: {current!r}")
    stable = config_without_metadata(current.get("stable"))
    pending = current.get("pending")
    pending_config = config_without_metadata(pending) if pending is not None else None
    if stable == http_config and pending is None:
        return
    if pending_config == http_config and current.get("active_config_type") == "pending":
        await websocket_command(session, settings, token, {"id": 1, "type": "http/config/promote"})
        return

    result = await websocket_command(
        session, settings, token, {"id": 1, "type": "http/config/configure", "config": http_config}
    )
    if not isinstance(result, dict) or not isinstance(result.get("restart"), bool):
        raise TypeError(f"Home Assistant returned an invalid HTTP configure response: {result!r}")
    if not result["restart"]:
        return

    await wait_for_home_assistant(session, settings)
    refreshed_token = await exchange_token(session, settings, await login(session, settings, password))
    await websocket_command(session, settings, refreshed_token, {"id": 1, "type": "http/config/promote"})


async def provision(session: aiohttp.ClientSession, settings: ProvisionerSettings, password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    completed = await wait_for_home_assistant(session, settings)
    required_steps = settings.required_onboarding_steps
    if completed is None or completed >= required_steps:
        token = await exchange_token(session, settings, await login(session, settings, password))
        await configure_http(session, settings, password, token)
        print("Home Assistant onboarding is already complete")
        return

    auth_code = (
        await login(session, settings, password)
        if "user" in completed
        else await create_owner(session, settings, password)
    )
    token = await exchange_token(session, settings, auth_code)
    if "core_config" not in completed:
        await request_json(session, settings, "/api/onboarding/core_config", data={}, token=token)
    if "integration" not in completed:
        await request_json(
            session,
            settings,
            "/api/onboarding/integration",
            data={"client_id": settings.client_id, "redirect_uri": settings.redirect_uri},
            token=token,
        )
    if "analytics" not in completed:
        await request_json(session, settings, "/api/onboarding/analytics", data={}, token=token)
    await configure_http(session, settings, password, token)
    print("Home Assistant onboarding is complete")


async def main() -> None:
    """Install configured components and complete configured onboarding."""
    settings = load_settings()
    config_dir = Path("/config")
    async with aiohttp.ClientSession() as session:
        await install_components(session, config_dir, settings.components)
        if settings.onboarding_enabled:
            if settings.local_admin_password is None:
                raise ValueError("HOME_ASSISTANT_PROVISIONER_LOCAL_ADMIN_PASSWORD is required for onboarding")
            await provision(session, settings, settings.local_admin_password.get_secret_value())


if __name__ == "__main__":
    asyncio.run(main())
