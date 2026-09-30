"""OAuth for a dedicated Claude grant: PKCE pairing, the credential file, and a self-refreshing token source.

The credential file has one owning process: refresh tokens rotate, so two holders would invalidate each other.
"""

import asyncio
import base64
import hashlib
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable, Sequence, Set
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

from devinfra.claude.claude_api.oauth_client import AUTHORIZE_URL, CLIENT_ID, TOKEN_URL
from devinfra.claude.session_export.failures import raise_for_status_with_body

logger = logging.getLogger(__name__)

CALLBACK_PORT = 54545  # the port of the redirect URI registered for the public client
# The narrowest scopes worth trying first; enough to list sessions and read events.
DEFAULT_SCOPES = ("user:profile", "user:sessions:claude_code")
REFRESH_SKEW = timedelta(minutes=5)


class Organization(BaseModel):
    uuid: str


class TokenBearer(BaseModel):
    """Models that hold tokens: a validation error must not echo the rejected input into a terminal or a paste."""

    model_config = ConfigDict(hide_input_in_errors=True)


class RefreshedTokens(TokenBearer):
    access_token: SecretStr
    refresh_token: SecretStr | None = Field(default=None, description="Absent when the server did not rotate it.")
    expires_in: int


class PairedTokens(TokenBearer):
    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int
    scope: str | None = Field(default=None, description="The granted scopes, if the server reports them.")
    organization: Organization


class OAuthCredential(TokenBearer, frozen=True):
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: datetime
    scopes: Set[str]
    organization_uuid: str

    @field_serializer("access_token", "refresh_token", when_used="json")
    def _reveal(self, secret: SecretStr) -> str:
        return secret.get_secret_value()  # only CredentialStore.save dumps JSON


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> OAuthCredential:
        return OAuthCredential.model_validate_json(self._path.read_text())

    def exists(self) -> bool:
        return self._path.exists()

    def save(self, credential: OAuthCredential) -> None:
        partial = self._path.with_name(f"{self._path.name}.part")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)  # a leftover .part may predate this call
            f.write(credential.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        partial.replace(self._path)


async def _post_token(client: httpx.AsyncClient, body: dict[str, str]) -> httpx.Response:
    response = await client.post(TOKEN_URL, json=body)
    raise_for_status_with_body(response)
    return response


async def refresh(client: httpx.AsyncClient, credential: OAuthCredential) -> OAuthCredential:
    response = await _post_token(
        client,
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token.get_secret_value(),
            "scope": " ".join(sorted(credential.scopes)),
        },
    )
    tokens = RefreshedTokens.model_validate_json(response.content)
    return credential.model_copy(
        update={
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token or credential.refresh_token,
            "expires_at": datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        }
    )


class OAuthTokenSource:
    """Hands out a valid access token, refreshing and persisting the rotated credential when it nears expiry."""

    def __init__(self, store: CredentialStore, client: httpx.AsyncClient) -> None:
        self._store = store
        self._client = client
        self._credential = store.load()
        self._refreshing = asyncio.Lock()

    @property
    def organization_uuid(self) -> str:
        return self._credential.organization_uuid

    async def access_token(self) -> str:
        async with self._refreshing:
            if self._credential.expires_at - datetime.now(UTC) <= REFRESH_SKEW:
                self._credential = await refresh(self._client, self._credential)
                # Persist before use: the old refresh token may already be dead.
                self._store.save(self._credential)
            return self._credential.access_token.get_secret_value()


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/callback"


def authorization_url(*, state: str, challenge: str, scopes: Sequence[str], port: int) -> str:
    query = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri(port),
        "scope": " ".join(scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


@asynccontextmanager
async def callback_listener(*, state: str, port: int) -> AsyncIterator[asyncio.Future[str]]:
    """Serve loopback `/callback`; the future resolves to the authorization code once the browser delivers it.

    A request with the wrong `state` is refused and ignored, so a stray tab cannot abort a pairing.
    """
    outcome: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def respond(writer: asyncio.StreamWriter, status: str, message: str) -> None:
        body = message.encode()
        header = f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        writer.write(header.encode() + body)
        await writer.drain()
        writer.close()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode("latin-1").split()
        target = urlsplit(request_line[1] if len(request_line) >= 2 else "")
        query = parse_qs(target.query)
        got_state = query.get("state", [""])[0]
        if target.path != "/callback" or not secrets.compare_digest(got_state.encode(), state.encode()):
            await respond(writer, "400 Bad Request", "not the pairing callback")
        elif "code" in query and not outcome.done():
            outcome.set_result(query["code"][0])
            await respond(writer, "200 OK", "Paired. You can close this tab.")
        elif not outcome.done():
            outcome.set_exception(ValueError(f"authorization refused: {query.get('error', ['no code returned'])[0]}"))
            await respond(writer, "400 Bad Request", "Authorization refused; see the terminal.")
        else:
            await respond(writer, "400 Bad Request", "already paired")

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    async with server:
        yield outcome


@dataclass(frozen=True)
class PairingAttempt:
    """One authorization-code + PKCE attempt: the verifier and state its redemption has to present."""

    verifier: str
    state: str
    scopes: tuple[str, ...]
    port: int

    @classmethod
    def start(cls, *, scopes: Sequence[str], port: int) -> Self:
        return cls(verifier=secrets.token_urlsafe(64), state=secrets.token_urlsafe(32), scopes=tuple(scopes), port=port)

    @property
    def url(self) -> str:
        return authorization_url(
            state=self.state, challenge=pkce_challenge(self.verifier), scopes=self.scopes, port=self.port
        )

    def code_from_redirect(self, redirect_url: str) -> str:
        """The authorization code in the URL the browser was sent to after approval. Nothing listens on the loopback
        redirect, so the page fails to load and the URL is read from the address bar. Errors do not echo the URL."""
        query = parse_qs(urlsplit(redirect_url.strip()).query)
        if not secrets.compare_digest(query.get("state", [""])[0].encode(), self.state.encode()):
            raise ValueError("the pasted URL is not from this pairing attempt: its state differs")
        if "code" not in query:
            raise ValueError(f"authorization refused: {query.get('error', ['no code returned'])[0]}")
        return query["code"][0]


async def redeem(client: httpx.AsyncClient, attempt: PairingAttempt, code: str) -> OAuthCredential:
    response = await _post_token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(attempt.port),
            "client_id": CLIENT_ID,
            "code_verifier": attempt.verifier,
            "state": attempt.state,
        },
    )
    tokens = PairedTokens.model_validate_json(response.content)
    return OAuthCredential(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        scopes=frozenset(tokens.scope.split()) if tokens.scope else frozenset(attempt.scopes),
        organization_uuid=tokens.organization.uuid,
    )


async def pair(
    client: httpx.AsyncClient,
    store: CredentialStore,
    *,
    scopes: Sequence[str],
    port: int,
    announce: Callable[[str], None],
) -> OAuthCredential:
    """Run the authorization-code + PKCE flow once through a loopback listener and save the credential.

    `announce` receives the URL the human must open; the listener is already serving when it is called.
    Waits for the browser without limit: bound it with `asyncio.timeout`.
    """
    attempt = PairingAttempt.start(scopes=scopes, port=port)
    async with callback_listener(state=attempt.state, port=port) as code_future:
        announce(attempt.url)
        code = await code_future
    credential = await redeem(client, attempt, code)
    store.save(credential)
    logger.info(
        "paired organization=%s scopes=%s expires_at=%s",
        credential.organization_uuid,
        " ".join(sorted(credential.scopes)),
        credential.expires_at.isoformat(),
    )
    return credential
