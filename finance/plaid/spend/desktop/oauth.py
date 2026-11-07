"""Authentik authorization-code + PKCE login and access-token refresh."""

from __future__ import annotations

import asyncio
import hmac
import html
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from aiohttp import web
from authlib.common.security import generate_token
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client

from .credentials import SecretServiceTokenStore

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 43821
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    client_id: str

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


@dataclass(frozen=True)
class CallbackResult:
    code: str | None = None
    error: str | None = None
    error_description: str | None = None


def _display_text(value: str, limit: int) -> str:
    """Keep callback errors safe for both HTML and terminal display."""
    return "".join(character for character in value if character.isprintable()).strip()[:limit]


def _callback_error(query: dict[str, list[str]]) -> CallbackResult | None:
    error = _display_text(query.get("error", [""])[0], 80)
    if not error:
        return None
    description = _display_text(query.get("error_description", [""])[0], 240)
    return CallbackResult(error=error, error_description=description or None)


def _format_callback_error(callback: CallbackResult) -> str:
    error = callback.error or "unknown_error"
    message = f"Authentik rejected Plaid Spend sign-in ({error})"
    if callback.error_description:
        message += f": {callback.error_description}"
    if error == "invalid_request":
        message += ". Check that the Authentik provider allows the authorization_code grant"
    return message


def _validate_token_response(response: httpx2.Response) -> httpx2.Response:
    response.raise_for_status()
    if not isinstance(response.json(), dict):
        raise RuntimeError("Authentik returned an invalid token response")
    return response


def _callback_response(status: HTTPStatus, message: str) -> web.Response:
    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Plaid Spend</title></head>'
        f"<body><p>{html.escape(message, quote=False)}</p></body></html>"
    ).encode()
    return web.Response(
        status=status,
        body=body,
        headers={
            "Cache-Control": "no-store",
            "Connection": "close",
            "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "Content-Type": "text/html; charset=utf-8",
        },
    )


def _callback_app(state: str, callback_future: asyncio.Future[CallbackResult]) -> web.Application:
    async def handle_callback(request: web.Request) -> web.Response:
        if request.method != "GET":
            return _callback_response(HTTPStatus.BAD_REQUEST, "Invalid callback request.")

        if request.path != CALLBACK_PATH:
            return _callback_response(HTTPStatus.NOT_FOUND, "Unknown callback path.")

        query = parse_qs(request.rel_url.raw_query_string, keep_blank_values=True)
        returned_state = query.get("state", [""])[0]
        if not hmac.compare_digest(returned_state, state):
            if not callback_future.done():
                callback_future.set_exception(RuntimeError("Authentik returned an invalid login state"))
            return _callback_response(
                HTTPStatus.BAD_REQUEST, "Login state did not match. Close this tab and try again."
            )

        callback_error = _callback_error(query)
        if callback_error is not None:
            if not callback_future.done():
                callback_future.set_result(callback_error)
            return _callback_response(
                HTTPStatus.OK, f"{_format_callback_error(callback_error)}. You can close this tab."
            )

        code = query.get("code", [""])[0]
        if not code:
            if not callback_future.done():
                callback_future.set_exception(RuntimeError("Authentik did not return an authorization code"))
            return _callback_response(HTTPStatus.BAD_REQUEST, "No authorization code was returned.")

        if not callback_future.done():
            callback_future.set_result(CallbackResult(code=code))
        return _callback_response(HTTPStatus.OK, "Plaid Spend is connected. You can close this tab.")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle_callback)
    return app


class Authenticator:
    def __init__(self, config: OAuthConfig, http: httpx.AsyncClient, token_store: SecretServiceTokenStore) -> None:
        self.config = config
        self.http = http
        self.token_store = token_store
        self.access_token: str | None = None
        self.access_token_expires_at = 0.0
        self._discovery: dict[str, str] | None = None

    def _oauth_client(self) -> AsyncOAuth2Client:
        client = AsyncOAuth2Client(
            client_id=self.config.client_id,
            redirect_uri=REDIRECT_URI,
            token_endpoint_auth_method="none",
            code_challenge_method="S256",
            timeout=httpx2.Timeout(connect=15, read=15, write=15, pool=15),
            follow_redirects=False,
        )
        client.register_compliance_hook("access_token_response", _validate_token_response)
        return client

    async def _metadata(self) -> dict[str, str]:
        if self._discovery is None:
            response = await self.http.get(self.config.discovery_url)
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("issuer", "").rstrip("/") != self.config.issuer.rstrip("/"):
                raise RuntimeError("Authentik discovery issuer did not match the configured issuer")
            for key in ("authorization_endpoint", "token_endpoint"):
                value = metadata.get(key)
                if not isinstance(value, str) or urlsplit(value).scheme != "https":
                    raise RuntimeError(f"Authentik discovery did not provide an HTTPS {key}")
            self._discovery = metadata
        return self._discovery

    async def login(self) -> None:
        metadata = await self._metadata()
        state = generate_token(48)
        verifier = generate_token(48)
        callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()
        runner = web.AppRunner(_callback_app(state, callback_future), access_log=None)
        try:
            try:
                await runner.setup()
                site = web.TCPSite(runner, CALLBACK_HOST, CALLBACK_PORT)
                await site.start()
            except OSError as exc:
                raise RuntimeError(f"could not listen on the Plaid Spend sign-in callback at {REDIRECT_URI}") from exc

            async with self._oauth_client() as oauth:
                authorization_url, _ = oauth.create_authorization_url(
                    metadata["authorization_endpoint"],
                    state=state,
                    code_verifier=verifier,
                    scope="openid offline_access",
                )
            process = await asyncio.create_subprocess_exec(
                "xdg-open", authorization_url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode not in (0, None):
                raise RuntimeError("the browser launcher could not open Authentik")
            callback = await asyncio.wait_for(callback_future, timeout=300)
        finally:
            await runner.cleanup()

        if callback.error:
            raise RuntimeError(_format_callback_error(callback))
        if callback.code is None:
            raise RuntimeError("Authentik sign-in did not return a code")

        token_response = await self._fetch_token(
            metadata["token_endpoint"],
            grant_type="authorization_code",
            code=callback.code,
            redirect_uri=REDIRECT_URI,
            code_verifier=verifier,
        )
        refresh_token = token_response.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError(
                "Authentik did not issue a refresh token; offline_access must be enabled for this client"
            )
        self._accept_token_response(token_response)
        await self.token_store.save_refresh_token(refresh_token)

    async def access_token_for_api(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self.access_token and self.access_token_expires_at - time.monotonic() > 45:
            return self.access_token

        refresh_token = await self.token_store.load_refresh_token()
        if refresh_token is None:
            self.access_token = None
            self.access_token_expires_at = 0.0
            raise AuthenticationRequiredError("Plaid Spend needs you to sign in")

        metadata = await self._metadata()
        try:
            token_response = await self._fetch_token(
                metadata["token_endpoint"], grant_type="refresh_token", refresh_token=refresh_token
            )
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                await self.token_store.clear()
                self.access_token = None
                self.access_token_expires_at = 0.0
                raise AuthenticationRequiredError("Plaid Spend sign-in expired; sign in again") from exc
            raise

        rotated_refresh_token = token_response.get("refresh_token")
        if isinstance(rotated_refresh_token, str) and rotated_refresh_token:
            await self.token_store.save_refresh_token(rotated_refresh_token)
        self._accept_token_response(token_response)
        if not self.access_token:
            raise RuntimeError("Authentik token response did not contain an access token")
        return self.access_token

    async def _fetch_token(self, endpoint: str, **grant_parameters: str) -> dict[str, object]:
        try:
            async with self._oauth_client() as oauth:
                token_response = await oauth.fetch_token(endpoint, **grant_parameters)
        except OAuthError:
            raise RuntimeError("Authentik rejected the token request") from None
        if not isinstance(token_response, dict):
            raise RuntimeError("Authentik returned an invalid token response")
        return token_response

    def _accept_token_response(self, token_response: dict[str, object]) -> None:
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Authentik token response did not contain an access token")
        raw_expires_in = token_response.get("expires_in", 300)
        try:
            if not isinstance(raw_expires_in, (int, str)):
                raise TypeError("expires_in must be an integer or string")
            expires_in = max(0, int(raw_expires_in))
        except TypeError, ValueError:
            expires_in = 300
        self.access_token = access_token
        self.access_token_expires_at = time.monotonic() + expires_in


class AuthenticationRequiredError(RuntimeError):
    """Raised when there is no valid refresh credential for the service."""
