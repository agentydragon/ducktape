"""Async Home Assistant HTTP and WebSocket API client."""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from urllib import parse

import aiohttp
import httpx2
from pydantic import BaseModel, StrictBool, TypeAdapter
from settings import ProvisionerSettings
from tenacity import AsyncRetrying, retry_if_exception, stop_after_delay, wait_fixed


class OnboardingStep(StrEnum):
    """Known Home Assistant onboarding API steps."""

    USER = "user"
    CORE_CONFIG = "core_config"
    INTEGRATION = "integration"
    ANALYTICS = "analytics"


class OnboardingStepStatus(BaseModel):
    """Validated status returned by the Home Assistant onboarding API."""

    step: OnboardingStep
    done: StrictBool


def _is_retryable_readiness_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code != HTTPStatus.UNAUTHORIZED
    return isinstance(exc, (httpx2.TransportError, TimeoutError))


class HomeAssistantClient:
    """Home Assistant API operations using injected HTTP and WebSocket clients."""

    readiness_timeout_secs = 300
    readiness_retry_interval_secs = 5

    def __init__(
        self, http_client: httpx2.AsyncClient, websocket_session: aiohttp.ClientSession, settings: ProvisionerSettings
    ) -> None:
        self.http_client = http_client
        self.websocket_session = websocket_session
        self.settings = settings
        self._access_token: str | None = None

    async def request_json(
        self, path: str, *, data: dict[str, object] | None = None, authenticated: bool = True, form: bool = False
    ) -> object:
        """Send an API request and decode its JSON response."""
        headers = {"Accept": "application/json"}
        method = "GET" if data is None else "POST"
        if authenticated:
            if self._access_token is None:
                raise RuntimeError("Home Assistant client has no access token; log in first")
            headers["Authorization"] = f"Bearer {self._access_token}"
        url = f"{self.settings.home_assistant_url}{path}"
        if data is None:
            response = await self.http_client.request(method, url, headers=headers, timeout=30)
        elif form:
            response = await self.http_client.request(method, url, headers=headers, timeout=30, data=data)
        else:
            response = await self.http_client.request(method, url, headers=headers, timeout=30, json=data)
        response.raise_for_status()
        return response.json()

    async def wait_until_ready(self) -> set[OnboardingStep] | None:
        """Wait for the API and return completed onboarding steps, or None if complete."""
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(self.readiness_timeout_secs),
                wait=wait_fixed(self.readiness_retry_interval_secs),
                retry=retry_if_exception(_is_retryable_readiness_error),
                reraise=True,
            ):
                with attempt:
                    await self._verify_api_ready()
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.UNAUTHORIZED:
                return await self.onboarding_status()
            raise TimeoutError("Home Assistant did not become available within 5 minutes") from exc
        except (httpx2.TransportError, TimeoutError) as exc:
            raise TimeoutError("Home Assistant did not become available within 5 minutes") from exc
        raise RuntimeError("Home Assistant API unexpectedly allowed an unauthenticated request")

    async def _verify_api_ready(self) -> None:
        await self.request_json("/api/", authenticated=False)

    async def onboarding_status(self) -> set[OnboardingStep] | None:
        """Return completed onboarding steps, or None when onboarding views are absent."""
        try:
            response = await self.request_json("/api/onboarding", authenticated=False)
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise
        statuses = TypeAdapter(list[OnboardingStepStatus]).validate_python(response)
        return {status.step for status in statuses if status.done}

    @staticmethod
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

    async def create_owner(self, password: str) -> None:
        """Create the local owner and authenticate it for subsequent API calls."""
        response = await self.request_json(
            "/api/onboarding/users",
            authenticated=False,
            data={
                "name": self.settings.display_name,
                "username": self.settings.username,
                "password": password,
                "client_id": self.settings.client_id,
                "language": "en",
            },
        )
        auth_code = self.required_string(response, "auth_code")
        await self._exchange_token(auth_code)

    async def login(self, password: str) -> None:
        """Authenticate the local owner after a partially completed run."""
        response = await self.request_json(
            "/auth/login_flow",
            authenticated=False,
            data={
                "client_id": self.settings.client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": self.settings.redirect_uri,
            },
        )
        flow_id = self.required_string(response, "flow_id")
        response = await self.request_json(
            f"/auth/login_flow/{flow_id}",
            authenticated=False,
            data={"client_id": self.settings.client_id, "username": self.settings.username, "password": password},
        )
        auth_code = self.required_string(response, "result")
        await self._exchange_token(auth_code)

    async def _exchange_token(self, auth_code: str) -> None:
        """Exchange an authorization code for an access token."""
        response = await self.request_json(
            "/auth/token",
            authenticated=False,
            data={"grant_type": "authorization_code", "code": auth_code, "client_id": self.settings.client_id},
            form=True,
        )
        self._access_token = self.required_string(response, "access_token")

    def websocket_url(self) -> str:
        """Return the Home Assistant WebSocket API URL."""
        parsed = parse.urlsplit(self.settings.home_assistant_url)
        websocket_scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
        if websocket_scheme is None:
            raise ValueError(f"Unsupported Home Assistant URL scheme: {parsed.scheme}")
        return parse.urlunsplit((websocket_scheme, parsed.netloc, "/api/websocket", "", ""))

    async def websocket_command(self, message: dict[str, object]) -> object:
        """Authenticate to Home Assistant and execute one WebSocket command."""
        if self._access_token is None:
            raise RuntimeError("Home Assistant client has no access token; log in first")
        async with self.websocket_session.ws_connect(
            self.websocket_url(), timeout=aiohttp.ClientWSTimeout(ws_receive=30)
        ) as websocket:
            auth_required = await websocket.receive_json()
            if not isinstance(auth_required, dict) or auth_required.get("type") != "auth_required":
                raise RuntimeError(f"Home Assistant WebSocket did not request authentication: {auth_required!r}")
            await websocket.send_json({"type": "auth", "access_token": self._access_token})
            auth_result = await websocket.receive_json()
            if not isinstance(auth_result, dict) or auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant WebSocket authentication failed: {auth_result!r}")
            await websocket.send_json(message)
            result = await websocket.receive_json()
        if not isinstance(result, dict) or result.get("type") != "result" or result.get("success") is not True:
            raise RuntimeError(f"Home Assistant WebSocket command failed: {result!r}")
        return result.get("result")

    @staticmethod
    def config_without_metadata(config: object) -> dict[str, object]:
        """Validate and remove runtime metadata from a stored HTTP config."""
        if not isinstance(config, dict):
            raise TypeError(f"Home Assistant returned an invalid HTTP config: {config!r}")
        return {key: value for key, value in config.items() if key not in {"created_at", "error", "error_message"}}

    async def configure_http(self, password: str) -> None:
        """Converge Home Assistant's UI-managed HTTP settings through its admin API."""
        http_config = self.settings.http_config.model_dump()
        current = await self.websocket_command({"id": 1, "type": "http/config"})
        if not isinstance(current, dict):
            raise TypeError(f"Home Assistant returned an invalid HTTP config response: {current!r}")
        stable = self.config_without_metadata(current.get("stable"))
        pending = current.get("pending")
        pending_config = self.config_without_metadata(pending) if pending is not None else None
        if stable == http_config and pending is None:
            return
        if pending_config == http_config and current.get("active_config_type") == "pending":
            await self.websocket_command({"id": 1, "type": "http/config/promote"})
            return

        result = await self.websocket_command({"id": 1, "type": "http/config/configure", "config": http_config})
        if not isinstance(result, dict) or not isinstance(result.get("restart"), bool):
            raise TypeError(f"Home Assistant returned an invalid HTTP configure response: {result!r}")
        if not result["restart"]:
            return

        await self.wait_until_ready()
        await self.login(password)
        await self.websocket_command({"id": 1, "type": "http/config/promote"})
