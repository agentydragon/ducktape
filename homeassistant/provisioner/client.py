"""Async Home Assistant HTTP and WebSocket API client."""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from urllib import parse

import httpx2
from pydantic import BaseModel, StrictBool, TypeAdapter
from tenacity import AsyncRetrying, retry_if_exception, stop_after_delay, wait_fixed

from homeassistant.provisioner.endpoint import HomeAssistantEndpoint

# httpx2's optional WebSocket implementation, which `websocket_command` needs.
# gazelle:include_dep @pypi//wsproto


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
    """Home Assistant API operations using an injected HTTPX2 client."""

    readiness_timeout_secs = 300
    readiness_retry_interval_secs = 5

    def __init__(self, http_client: httpx2.AsyncClient, endpoint: HomeAssistantEndpoint) -> None:
        self.http_client = http_client
        self.endpoint = endpoint
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
        url = f"{self.endpoint.url}{path}"
        if data is None:
            response = await self.http_client.request(method, url, headers=headers, timeout=30)
        elif form:
            response = await self.http_client.request(method, url, headers=headers, timeout=30, data=data)
        else:
            response = await self.http_client.request(method, url, headers=headers, timeout=30, json=data)
        response.raise_for_status()
        return response.json()

    async def wait_until_ready(self) -> frozenset[OnboardingStep]:
        """Wait for the API and return the onboarding steps still pending."""
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
                return await self.pending_onboarding_steps()
            raise TimeoutError("Home Assistant did not become available within 5 minutes") from exc
        except (httpx2.TransportError, TimeoutError) as exc:
            raise TimeoutError("Home Assistant did not become available within 5 minutes") from exc
        raise RuntimeError("Home Assistant API unexpectedly allowed an unauthenticated request")

    async def _verify_api_ready(self) -> None:
        await self.request_json("/api/", authenticated=False)

    async def token_is_valid(self, token: str) -> bool:
        """Whether Home Assistant accepts `token`."""
        response = await self.http_client.get(
            f"{self.endpoint.url}/api/", headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return False
        response.raise_for_status()
        return True

    async def pending_onboarding_steps(self) -> frozenset[OnboardingStep]:
        """The onboarding steps not yet done: none once Home Assistant stops serving its onboarding
        views, which it does when onboarding is complete."""
        try:
            response = await self.request_json("/api/onboarding", authenticated=False)
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                return frozenset()
            raise
        statuses = TypeAdapter(list[OnboardingStepStatus]).validate_python(response)
        return frozenset(OnboardingStep) - {status.step for status in statuses if status.done}

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

    async def create_owner(self, name: str, username: str, password: str) -> None:
        """Create the local owner and authenticate it for subsequent API calls."""
        response = await self.request_json(
            "/api/onboarding/users",
            authenticated=False,
            data={
                "name": name,
                "username": username,
                "password": password,
                "client_id": self.endpoint.client_id,
                "language": "en",
            },
        )
        auth_code = self.required_string(response, "auth_code")
        await self._exchange_token(auth_code)

    async def login(self, username: str, password: str) -> None:
        """Authenticate `username` for subsequent API calls."""
        response = await self.request_json(
            "/auth/login_flow",
            authenticated=False,
            data={
                "client_id": self.endpoint.client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": self.endpoint.redirect_uri,
            },
        )
        flow_id = self.required_string(response, "flow_id")
        response = await self.request_json(
            f"/auth/login_flow/{flow_id}",
            authenticated=False,
            data={"client_id": self.endpoint.client_id, "username": username, "password": password},
        )
        auth_code = self.required_string(response, "result")
        await self._exchange_token(auth_code)

    async def _exchange_token(self, auth_code: str) -> None:
        """Exchange an authorization code for an access token."""
        response = await self.request_json(
            "/auth/token",
            authenticated=False,
            data={"grant_type": "authorization_code", "code": auth_code, "client_id": self.endpoint.client_id},
            form=True,
        )
        self._access_token = self.required_string(response, "access_token")

    def websocket_url(self) -> str:
        """Return the Home Assistant WebSocket API URL."""
        parsed = parse.urlsplit(self.endpoint.url)
        websocket_scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
        if websocket_scheme is None:
            raise ValueError(f"Unsupported Home Assistant URL scheme: {parsed.scheme}")
        return parse.urlunsplit((websocket_scheme, parsed.netloc, "/api/websocket", "", ""))

    async def websocket_command(self, message: dict[str, object]) -> object:
        """Authenticate to Home Assistant and execute one WebSocket command."""
        if self._access_token is None:
            raise RuntimeError("Home Assistant client has no access token; log in first")
        async with self.http_client.websocket(self.websocket_url()) as websocket:
            auth_required = await websocket.receive_json(timeout=30)
            if not isinstance(auth_required, dict) or auth_required.get("type") != "auth_required":
                raise RuntimeError(f"Home Assistant WebSocket did not request authentication: {auth_required!r}")
            await websocket.send_json({"type": "auth", "access_token": self._access_token})
            auth_result = await websocket.receive_json(timeout=30)
            if not isinstance(auth_result, dict) or auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant WebSocket authentication failed: {auth_result!r}")
            await websocket.send_json(message)
            result = await websocket.receive_json(timeout=30)
        if not isinstance(result, dict) or result.get("type") != "result" or result.get("success") is not True:
            raise RuntimeError(f"Home Assistant WebSocket command failed: {result!r}")
        return result.get("result")
