"""Authentik authorization-code + PKCE login and access-token refresh."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from plaid_spend_desktop.credentials import SecretServiceTokenStore

logger = logging.getLogger(__name__)

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 43821
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _random_urlsafe(byte_count: int = 32) -> str:
    return _b64url(secrets.token_bytes(byte_count))


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


class Authenticator:
    def __init__(self, config: OAuthConfig, http: httpx.AsyncClient, token_store: SecretServiceTokenStore) -> None:
        self.config = config
        self.http = http
        self.token_store = token_store
        self.access_token: str | None = None
        self.access_token_expires_at = 0.0
        self._discovery: dict[str, str] | None = None

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
        state = _random_urlsafe()
        verifier = _random_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()

        async def handle_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=10)
                parts = request_line.decode("latin-1").strip().split(" ", 2)
                if len(parts) != 3 or parts[0] != "GET":
                    await self._write_callback_response(writer, HTTPStatus.BAD_REQUEST, "Invalid callback request.")
                    return

                request_target = urlsplit(parts[1])
                if request_target.path != CALLBACK_PATH:
                    await self._write_callback_response(writer, HTTPStatus.NOT_FOUND, "Unknown callback path.")
                    return

                # Consume headers before returning a response, but never log the request
                # line: the callback URL contains the authorization code.
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=10)
                    if line in (b"\r\n", b"\n", b""):
                        break

                query = parse_qs(request_target.query, keep_blank_values=True)
                returned_state = query.get("state", [""])[0]
                if not hmac.compare_digest(returned_state, state):
                    await self._write_callback_response(
                        writer, HTTPStatus.BAD_REQUEST, "Login state did not match. Close this tab and try again."
                    )
                    if not callback_future.done():
                        callback_future.set_exception(RuntimeError("Authentik returned an invalid login state"))
                    return

                error = query.get("error", [""])[0]
                if error:
                    if not callback_future.done():
                        callback_future.set_result(CallbackResult(error=error[:80]))
                    await self._write_callback_response(
                        writer, HTTPStatus.OK, "Plaid Spend sign-in was not completed. You can close this tab."
                    )
                    return

                code = query.get("code", [""])[0]
                if not code:
                    if not callback_future.done():
                        callback_future.set_exception(RuntimeError("Authentik did not return an authorization code"))
                    await self._write_callback_response(
                        writer, HTTPStatus.BAD_REQUEST, "No authorization code was returned."
                    )
                    return

                if not callback_future.done():
                    callback_future.set_result(CallbackResult(code=code))
                await self._write_callback_response(
                    writer, HTTPStatus.OK, "Plaid Spend is connected. You can close this tab."
                )
            except (TimeoutError, UnicodeDecodeError, ConnectionError) as exc:
                if not callback_future.done():
                    callback_future.set_exception(RuntimeError("Could not receive the local sign-in callback"))
                logger.debug("OAuth callback connection ended: %s", type(exc).__name__)
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()

        try:
            server = await asyncio.start_server(handle_callback, CALLBACK_HOST, CALLBACK_PORT)
        except OSError as exc:
            raise RuntimeError(f"could not listen on the Plaid Spend sign-in callback at {REDIRECT_URI}") from exc

        authorization_params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid offline_access",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorization_url = f"{metadata['authorization_endpoint']}?{urlencode(authorization_params)}"
        try:
            process = await asyncio.create_subprocess_exec(
                "xdg-open", authorization_url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode not in (0, None):
                raise RuntimeError("the browser launcher could not open Authentik")
            callback = await asyncio.wait_for(callback_future, timeout=300)
        finally:
            server.close()
            await server.wait_closed()

        if callback.error:
            raise RuntimeError(f"Authentik sign-in failed: {callback.error}")
        if callback.code is None:
            raise RuntimeError("Authentik sign-in did not return a code")

        token_response = await self._token_request(
            metadata["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "code": callback.code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
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
            token_response = await self._token_request(
                metadata["token_endpoint"],
                {"grant_type": "refresh_token", "client_id": self.config.client_id, "refresh_token": refresh_token},
            )
        except httpx.HTTPStatusError as exc:
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

    async def _token_request(self, endpoint: str, form: dict[str, str]) -> dict[str, object]:
        response = await self.http.post(endpoint, data=form)
        response.raise_for_status()
        token_response = response.json()
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

    @staticmethod
    async def _write_callback_response(writer: asyncio.StreamWriter, status: HTTPStatus, message: str) -> None:
        body = (
            '<!doctype html><html><head><meta charset="utf-8"><title>Plaid Spend</title></head>'
            f"<body><p>{message}</p></body></html>"
        ).encode()
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body
        writer.write(response)
        await writer.drain()


class AuthenticationRequiredError(RuntimeError):
    """Raised when there is no valid refresh credential for the service."""
