"""Tana MCP OAuth 2.1 + PKCE token broker.

Runs as a sidecar in the tana-mcp pod. Performs the full OAuth flow against
Tana's local MCP server (localhost:8262), obtains access + refresh tokens,
and writes them to a K8s secret. Refreshes automatically before expiry.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
from aiohttp import web
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException
from tenacity import retry, retry_if_exception_type, stop_never, wait_exponential

logger = logging.getLogger(__name__)

# Tana auto-approves OAuth clients with this name.
CLIENT_NAME = "Claude Code"

# PKCE code verifier length (RFC 7636 recommends 43-128).
CODE_VERIFIER_LENGTH = 128


@dataclass
class BrokerConfig:
    tana_url: str = "http://127.0.0.1:8262"
    callback_port: int = 9876
    secret_name: str = "tana-mcp-oauth-tokens"
    namespace: str = "tana-mcp"
    refresh_margin_seconds: int = 3600
    managed_by: str = "tana-token-broker"


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(96)[:CODE_VERIFIER_LENGTH]


def _compute_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _TanaNotReadyError(Exception):
    pass


@retry(
    retry=retry_if_exception_type((_TanaNotReadyError, httpx.ConnectError, httpx.HTTPError)),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_never,
    before_sleep=lambda rs: logger.info(f"Tana not ready, retry attempt {rs.attempt_number}"),
)
async def _wait_for_tana(http: httpx.AsyncClient, tana_url: str) -> None:
    resp = await http.get(f"{tana_url}/health")
    if resp.status_code != 200:
        raise _TanaNotReadyError(f"health returned {resp.status_code}")
    logger.info("Tana MCP server is healthy")


async def _register_client(http: httpx.AsyncClient, tana_url: str, redirect_uri: str) -> str:
    """Register an OAuth client with Tana. Returns the client_id."""
    resp = await http.post(
        f"{tana_url}/oauth/register", json={"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri]}
    )
    resp.raise_for_status()
    data = resp.json()
    client_id: str = data["client_id"]
    logger.info(f"Registered OAuth client {client_id=}")
    return client_id


_AUTH_CODE_TIMEOUT = 65.0


async def _capture_auth_code(port: int) -> str:
    """Start a one-shot HTTP server and wait for the OAuth callback with the auth code."""
    code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def handle_callback(request: web.Request) -> web.Response:
        code = request.query.get("code")
        error = request.query.get("error")
        if error:
            code_future.set_exception(RuntimeError(f"OAuth authorize error: {error}"))
            return web.Response(text=f"Error: {error}", status=400)
        if not code:
            return web.Response(text="Missing code parameter", status=400)
        code_future.set_result(code)
        return web.Response(text="Authorization successful. You can close this window.")

    app = web.Application()
    app.router.add_get("/callback", handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        return await asyncio.wait_for(code_future, timeout=_AUTH_CODE_TIMEOUT)
    finally:
        await runner.cleanup()


async def _authorize_pkce(
    http: httpx.AsyncClient, tana_url: str, client_id: str, redirect_uri: str, callback_port: int
) -> tuple[str, str]:
    """Run the PKCE authorization flow. Returns (auth_code, code_verifier)."""
    code_verifier = _generate_code_verifier()
    code_challenge = _compute_code_challenge(code_verifier)
    state = secrets.token_urlsafe(32)

    # Start the callback listener before hitting authorize
    capture_task = asyncio.create_task(_capture_auth_code(callback_port))

    # Small delay to ensure the listener is ready
    await asyncio.sleep(0.1)

    # Tana's /oauth/authorize auto-approves for "Claude Code" clients and
    # returns a 302 redirect to our callback with ?code=ac_...
    resp = await http.get(
        f"{tana_url}/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
        follow_redirects=False,
    )

    if resp.status_code == 302:
        # Tana redirects to our callback — httpx didn't follow it since we
        # said follow_redirects=False. We need to extract the code from the
        # Location header and feed it to our listener, or just parse it.
        location = resp.headers["location"]
        parsed = urlparse(location)
        qs = parse_qs(parsed.query)
        codes = qs.get("code", [])
        if codes:
            capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture_task
            logger.info("Got auth code from redirect Location header")
            return codes[0], code_verifier

    # If we didn't get a 302 or couldn't parse the code, wait for the callback
    auth_code = await capture_task
    logger.info("Got auth code from callback listener")
    return auth_code, code_verifier


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    token_type: str
    expires_at: str
    client_id: str


async def _exchange_code(
    http: httpx.AsyncClient, tana_url: str, client_id: str, auth_code: str, code_verifier: str, redirect_uri: str
) -> TokenResult:
    resp = await http.post(
        f"{tana_url}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    expires_at = str(int(time.time()) + data["expires_in"])
    return TokenResult(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type=data.get("token_type", "Bearer"),
        expires_at=expires_at,
        client_id=client_id,
    )


async def _refresh_token(http: httpx.AsyncClient, tana_url: str, client_id: str, refresh_token: str) -> TokenResult:
    resp = await http.post(
        f"{tana_url}/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )
    resp.raise_for_status()
    data = resp.json()
    expires_at = str(int(time.time()) + data["expires_in"])
    return TokenResult(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", refresh_token),
        token_type=data.get("token_type", "Bearer"),
        expires_at=expires_at,
        client_id=client_id,
    )


async def _write_secret(api: client.CoreV1Api, cfg: BrokerConfig, token: TokenResult) -> None:
    """Create or update the K8s secret with token data."""
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=cfg.secret_name, namespace=cfg.namespace, labels={"app.kubernetes.io/managed-by": cfg.managed_by}
        ),
        string_data={
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_type": token.token_type,
            "expires_at": token.expires_at,
            "client_id": token.client_id,
        },
        type="Opaque",
    )
    try:
        await api.read_namespaced_secret(cfg.secret_name, cfg.namespace)
        await api.replace_namespaced_secret(cfg.secret_name, cfg.namespace, secret)
        logger.info(f"Updated secret {cfg.namespace}/{cfg.secret_name}")
    except ApiException as e:
        if e.status == 404:
            await api.create_namespaced_secret(cfg.namespace, secret)
            logger.info(f"Created secret {cfg.namespace}/{cfg.secret_name}")
        else:
            raise


async def _read_existing_token(api: client.CoreV1Api, cfg: BrokerConfig) -> TokenResult | None:
    """Read existing token from K8s secret, if it exists."""
    try:
        secret = await api.read_namespaced_secret(cfg.secret_name, cfg.namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise

    if secret.data is None:
        return None

    decoded = {k: base64.b64decode(v).decode() for k, v in secret.data.items()}
    if "refresh_token" not in decoded or "access_token" not in decoded:
        return None

    return TokenResult(
        access_token=decoded["access_token"],
        refresh_token=decoded["refresh_token"],
        token_type=decoded.get("token_type", "Bearer"),
        expires_at=decoded.get("expires_at", "0"),
        client_id=decoded.get("client_id", ""),
    )


def _token_needs_refresh(token: TokenResult, margin_seconds: int) -> bool:
    try:
        expires_at = int(token.expires_at)
    except (ValueError, TypeError):
        return True
    return time.time() >= expires_at - margin_seconds


async def run_broker(cfg: BrokerConfig) -> None:
    """Main broker loop. Runs forever, obtaining and refreshing tokens."""
    config.load_incluster_config()
    k8s_api = client.CoreV1Api()
    redirect_uri = f"http://127.0.0.1:{cfg.callback_port}/callback"

    async with httpx.AsyncClient(timeout=30.0) as http:
        while True:
            try:
                await _wait_for_tana(http, cfg.tana_url)

                # Check for existing refresh token
                existing = await _read_existing_token(k8s_api, cfg)
                if existing and existing.refresh_token:
                    if _token_needs_refresh(existing, cfg.refresh_margin_seconds):
                        logger.info("Existing token near expiry, refreshing")
                        token = await _refresh_token(http, cfg.tana_url, existing.client_id, existing.refresh_token)
                        await _write_secret(k8s_api, cfg, token)
                    else:
                        logger.info("Existing token still valid")
                        token = existing

                    # Sleep until refresh needed
                    await _sleep_until_refresh(token, cfg.refresh_margin_seconds)
                    continue

                # Full OAuth flow: register -> authorize -> exchange
                client_id = await _register_client(http, cfg.tana_url, redirect_uri)
                auth_code, code_verifier = await _authorize_pkce(
                    http, cfg.tana_url, client_id, redirect_uri, cfg.callback_port
                )
                token = await _exchange_code(http, cfg.tana_url, client_id, auth_code, code_verifier, redirect_uri)
                await _write_secret(k8s_api, cfg, token)
                logger.info(f"Token written, expires_at={token.expires_at}")

                # Sleep until refresh needed
                await _sleep_until_refresh(token, cfg.refresh_margin_seconds)

            except Exception:
                logger.exception("Broker loop error, retrying in 30s")
                await asyncio.sleep(30)


async def _sleep_until_refresh(token: TokenResult, margin_seconds: int) -> None:
    """Sleep until the token needs refreshing."""
    try:
        expires_at = int(token.expires_at)
    except (ValueError, TypeError):
        await asyncio.sleep(60)
        return

    refresh_at = expires_at - margin_seconds
    sleep_seconds = max(refresh_at - time.time(), 60)
    logger.info(f"Sleeping {sleep_seconds:.0f}s until next refresh")
    await asyncio.sleep(sleep_seconds)
