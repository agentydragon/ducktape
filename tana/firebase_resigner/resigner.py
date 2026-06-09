"""Tana Firebase session re-signer.

Runs as a sidecar in the tana-mcp pod. When the in-pod Tana Desktop's
Firebase session is dead, this sidecar:

1. Reads the Firebase refresh token from a K8s Secret.
2. Swaps it for a fresh Firebase ID token via securetoken.googleapis.com.
   If the response includes a rotated refresh token, writes it back to the
   K8s Secret so the SOPS material can be reseeded later.
3. Uses the ID token to call Tana's `fetchCustomToken` Cloud Function, which
   returns a fresh Firebase custom token bound to the user's account.
4. POSTs `tana://auth?token=<customToken>&providerId=tanaFirebaseToken` to
   the localhost receiver that the desktop container's entrypoint exposes
   on `127.0.0.1:9090/reseed`. Electron's `second-instance` handler then
   routes the URL into the running renderer, which signs in via
   `signInWithCustomToken(...)` — Firebase issues a fresh refresh token at
   the renderer side and persists it to IndexedDB.

See cluster/docs/plans/tana_mcp_sane_signin.md for the wider design.
"""

import asyncio
import base64
import contextlib
import logging
import time
from dataclasses import dataclass

import httpx
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException
from tenacity import retry, retry_if_exception_type, stop_never, wait_exponential

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResignerConfig:
    # Firebase web API key — embedded in the Tana web bundle's
    # REACT_APP_FIREBASE_API_KEY. Web API keys are public identifiers per
    # Google's docs, so this is config, not a secret.
    api_key: str
    # Tana hosts Firebase callables behind its own domain (the bundle's
    # REACT_APP_FIREBASE_FUNCTIONS_URL), not the default GCF subdomain. The
    # `fetchCustomToken` callable's URL is therefore
    # `<functions_base>/fetchCustomToken`.
    fetch_custom_token_url: str = "https://app.tana.inc/functions/fetchCustomToken"

    # In-pod URLs.
    tana_health_url: str = "http://127.0.0.1:8262/health"
    tana_mcp_url: str = "http://127.0.0.1:8262/mcp"
    reseed_url: str = "http://127.0.0.1:9090/reseed"

    # Server-held Tana personal access token. When set, readiness also requires
    # that Tana's MCP server currently accepts it (see _pat_accepted): /health
    # alone reports the renderer is loaded but not that it will authenticate the
    # PAT, the failure that leaves the facade serving zero tools. None disables
    # the PAT check (falls back to /health only).
    pat: str | None = None

    # K8s secret holding the long-lived refresh token. Sidecar mounts this
    # so it can read on startup; writes go via the K8s API so rotations
    # land in cluster state.
    namespace: str = "tana-mcp"
    secret_name: str = "tana-firebase-refresh-token"
    secret_key: str = "refresh_token"

    # Loop pacing.
    healthy_poll_seconds: float = 60.0
    unhealthy_poll_seconds: float = 5.0
    unhealthy_threshold: int = 3
    request_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class FreshTokens:
    id_token: str
    refresh_token: str
    expires_in: int


_SecureTokenError = (httpx.ConnectError, httpx.HTTPError)


@retry(
    retry=retry_if_exception_type(_SecureTokenError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_never,
    before_sleep=lambda rs: logger.info(f"securetoken retry {rs.attempt_number}"),
)
async def _refresh_id_token(http: httpx.AsyncClient, cfg: ResignerConfig, refresh_token: str) -> FreshTokens:
    """Exchange a refresh token for a fresh ID token. May rotate the refresh token."""
    resp = await http.post(
        "https://securetoken.googleapis.com/v1/token",
        params={"key": cfg.api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    resp.raise_for_status()
    data = resp.json()
    return FreshTokens(
        id_token=data["id_token"], refresh_token=data["refresh_token"], expires_in=int(data["expires_in"])
    )


async def _fetch_custom_token(http: httpx.AsyncClient, cfg: ResignerConfig, id_token: str) -> str:
    """Call Tana's fetchCustomToken callable (Firebase callable wire shape)."""
    resp = await http.post(
        cfg.fetch_custom_token_url,
        json={"data": {}},
        headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    # Firebase callable wraps the response as {result: <returned>}, and the
    # function returns {customToken: <token>} (see
    # gaffer-private/tana/re/web/.../cloud_functions/client.js:538). The
    # `.data.customToken` shape in that file is the SDK-side unwrap; over
    # the wire it's `{"result": {"customToken": "..."}}`.
    return str(resp.json()["result"]["customToken"])


async def _deliver_to_pod(http: httpx.AsyncClient, cfg: ResignerConfig, custom_token: str) -> None:
    """Hand the custom token to the desktop container's localhost receiver."""
    url = f"tana://auth?token={custom_token}&providerId=tanaFirebaseToken"
    resp = await http.post(cfg.reseed_url, json={"url": url})
    resp.raise_for_status()


async def _is_tana_healthy(http: httpx.AsyncClient, cfg: ResignerConfig) -> bool:
    try:
        resp = await http.get(cfg.tana_health_url)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def _pat_accepted(http: httpx.AsyncClient, cfg: ResignerConfig) -> bool:
    """Whether Tana's MCP server currently authenticates the server-held PAT.

    Exercises the exact auth gate the public facade hits: a POST /mcp
    `initialize` with the PAT. HTTP 200 means the renderer's `validateToken`
    accepts the token; 401 means it is refusing it (typically because the
    renderer drifted off the matching account after a session blip), which a
    re-sign can recover. A successful probe opens an MCP session, so terminate
    it best-effort to avoid leaking one per poll.
    """
    assert cfg.pat is not None
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "firebase-resigner-probe", "version": "1"},
        },
    }
    headers = {
        "Authorization": f"Bearer {cfg.pat}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        resp = await http.post(cfg.tana_mcp_url, json=body, headers=headers)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    session_id = resp.headers.get("mcp-session-id")
    if session_id:
        with contextlib.suppress(httpx.HTTPError):
            await http.delete(cfg.tana_mcp_url, headers={"mcp-session-id": session_id})
    return True


async def _is_tana_ready(http: httpx.AsyncClient, cfg: ResignerConfig) -> bool:
    """Tana is ready when /health is up and (if configured) the PAT is accepted."""
    if not await _is_tana_healthy(http, cfg):
        return False
    if cfg.pat is None:
        return True
    if not await _pat_accepted(http, cfg):
        logger.info("Tana /health OK but MCP rejects the PAT; treating as unhealthy to drive a re-sign")
        return False
    return True


async def _read_refresh_token(api: client.CoreV1Api, cfg: ResignerConfig) -> str:
    secret = await api.read_namespaced_secret(cfg.secret_name, cfg.namespace)
    if secret.data is None or cfg.secret_key not in secret.data:
        raise RuntimeError(f"secret {cfg.namespace}/{cfg.secret_name} missing key {cfg.secret_key!r}")
    return base64.b64decode(secret.data[cfg.secret_key]).decode()


async def _write_rotated_refresh_token(api: client.CoreV1Api, cfg: ResignerConfig, new_token: str) -> None:
    """Patch the K8s Secret in place with the rotated refresh token."""
    body = {"stringData": {cfg.secret_key: new_token}}
    try:
        await api.patch_namespaced_secret(cfg.secret_name, cfg.namespace, body)
    except ApiException:
        logger.exception("failed to write rotated refresh token; continuing with the in-memory value")


async def _reseed_once(http: httpx.AsyncClient, k8s_api: client.CoreV1Api, cfg: ResignerConfig) -> None:
    refresh_token = await _read_refresh_token(k8s_api, cfg)
    fresh = await _refresh_id_token(http, cfg, refresh_token)
    if fresh.refresh_token != refresh_token:
        logger.info("refresh token rotated by Google; writing back to K8s secret")
        await _write_rotated_refresh_token(k8s_api, cfg, fresh.refresh_token)
    logger.info(f"got fresh ID token, expires_in={fresh.expires_in}s; calling fetchCustomToken")
    custom_token = await _fetch_custom_token(http, cfg, fresh.id_token)
    logger.info("got Tana custom token; delivering to desktop container")
    await _deliver_to_pod(http, cfg, custom_token)
    logger.info("delivered; waiting for in-pod renderer to complete signInWithCustomToken")


async def run_resigner(cfg: ResignerConfig) -> None:
    config.load_incluster_config()
    k8s_api = client.CoreV1Api()
    unhealthy_streak = 0
    last_reseed_at: float = 0.0
    async with httpx.AsyncClient(timeout=cfg.request_timeout_seconds) as http:
        while True:
            healthy = await _is_tana_ready(http, cfg)
            if healthy:
                if unhealthy_streak:
                    logger.info("Tana healthy again")
                unhealthy_streak = 0
                await asyncio.sleep(cfg.healthy_poll_seconds)
                continue

            unhealthy_streak += 1
            logger.info(f"Tana unhealthy ({unhealthy_streak}/{cfg.unhealthy_threshold})")
            if unhealthy_streak < cfg.unhealthy_threshold:
                await asyncio.sleep(cfg.unhealthy_poll_seconds)
                continue

            # Throttle reseeds: even if Tana stays unhealthy after delivery,
            # don't hammer the receiver while the renderer completes sign-in.
            since_reseed = time.monotonic() - last_reseed_at
            if since_reseed < cfg.healthy_poll_seconds:
                await asyncio.sleep(cfg.healthy_poll_seconds - since_reseed)
                continue

            try:
                await _reseed_once(http, k8s_api, cfg)
                last_reseed_at = time.monotonic()
            except Exception:
                logger.exception("reseed failed; backing off")
                await asyncio.sleep(cfg.healthy_poll_seconds)
