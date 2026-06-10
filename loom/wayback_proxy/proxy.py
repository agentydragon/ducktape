"""Date-clamping Wayback Machine forward proxy (the loom "time machine").

Plain-HTTP forward proxy: every absolute-URI request is answered with the
newest Internet Archive capture at-or-before ``WAYBACK_AS_OF``, served as raw
``id_`` bytes (no IA banner or rewriting). CONNECT is refused — HTTPS is
disabled by design; agents use ``http://`` URLs (IA serves the same snapshot
regardless of the original scheme). Emits a served-evidence JSONL manifest
line per response on stdout. See loom/plans/wayback_proxy.md.

Configuration (env): ``WAYBACK_AS_OF`` (required ISO date, inclusive),
``WAYBACK_UPSTREAM`` (default ``https://web.archive.org``; point at the
wayback-cache cluster service to share a pull-through cache), ``PORT``
(default 8080), ``WAYBACK_MANIFEST_PATH`` (write the manifest to this file
instead of stdout — the gym scorer reads it out of the sandbox per sample).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

import aiohttp
from aiohttp import web
from yarl import URL

logger = logging.getLogger(__name__)

CDX_PATH = "/cdx/search/cdx"
ARCHIVE_HOST = "web.archive.org"
MAX_REDIRECT_HOPS = 10
MAX_BODY_BYTES = 64 * 2**20

# "/web/<ts><modifier?>/<url>" — IA replay path. Modifiers are two letters
# plus underscore (id_, if_, im_, js_, cs_, ...).
_WEB_PATH_RE = re.compile(r"^/web/(\d{4,14})([a-z]{2}_)?/(.*)$")


class NoCaptureError(Exception):
    """No archived capture at or before as_of → HTTP 404."""


class ClampViolationError(Exception):
    """Request or archive redirect points after as_of → HTTP 403."""


class UpstreamError(Exception):
    """Archive/cache failed in an unexpected way → HTTP 502."""


def pad_ts(ts: str) -> str:
    """Right-pad a partial IA timestamp to 14 digits (earliest moment in the period)."""
    return ts.ljust(14, "0")


def ts_allowed(ts: str, as_of_ts: str) -> bool:
    return pad_ts(ts) <= as_of_ts


def parse_web_path(path_qs: str) -> tuple[str, str, str] | None:
    """Split "/web/<ts><modifier?>/<url>" into (ts, modifier, inner url)."""
    if (match := _WEB_PATH_RE.match(path_qs)) is None:
        return None
    ts, modifier, inner = match.groups()
    return ts, modifier or "", inner


def pick_capture(cdx_rows: list[list[str]]) -> tuple[str, str] | None:
    """Newest (timestamp, original) from a CDX ``output=json`` payload.

    Row 0 is the column-name header; with ``limit=-1`` the single data row is
    the newest capture ≤ ``to``. Returns None when no data rows exist.
    """
    if len(cdx_rows) < 2:
        return None
    header, *rows = cdx_rows
    ts_index, original_index = header.index("timestamp"), header.index("original")
    newest = rows[-1]
    return newest[ts_index], newest[original_index]


@dataclass(frozen=True)
class Config:
    as_of: date
    upstream: str  # base URL, no trailing slash
    port: int
    manifest_path: Path | None = None  # None: manifest lines go to stdout
    # Sent as the Authorization header on every upstream request (the proxy
    # only ever contacts `upstream`). Used when upstream is the authed public
    # route of the shared cache; None for direct IA or the unauthed ClusterIP.
    upstream_auth: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        as_of_raw = os.environ.get("WAYBACK_AS_OF")
        if as_of_raw is None:
            raise RuntimeError("WAYBACK_AS_OF must be set to the ISO information-cutoff date")
        manifest_raw = os.environ.get("WAYBACK_MANIFEST_PATH")
        return cls(
            as_of=date.fromisoformat(as_of_raw),
            upstream=os.environ.get("WAYBACK_UPSTREAM", f"https://{ARCHIVE_HOST}").rstrip("/"),
            port=int(os.environ.get("PORT", "8080")),
            manifest_path=Path(manifest_raw) if manifest_raw is not None else None,
            # Empty (the compose default when no auth) is treated as unset.
            upstream_auth=os.environ.get("WAYBACK_UPSTREAM_AUTH") or None,
        )

    @property
    def as_of_ts(self) -> str:
        """Inclusive 14-digit clamp: end of the as_of day (IA timestamps are UTC)."""
        return f"{self.as_of:%Y%m%d}235959"

    @property
    def upstream_host(self) -> str:
        host = URL(self.upstream).host
        if host is None:
            raise RuntimeError(f"WAYBACK_UPSTREAM has no host: {self.upstream!r}")
        return host


@dataclass(frozen=True)
class Snapshot:
    """One archive response: either a body to serve or an off-archive redirect."""

    ts: str
    status: int
    content_type: str
    body: bytes
    location: str | None = None


class WaybackProxy:
    def __init__(self, config: Config, session: aiohttp.ClientSession, manifest: TextIO) -> None:
        self._config = config
        self._session = session
        self._manifest = manifest

    async def handle(self, request: web.BaseRequest) -> web.StreamResponse:
        if request.method == "CONNECT":
            return web.Response(
                status=501,
                text="HTTPS tunneling is disabled; request the http:// form of the URL through this proxy.\n",
            )
        # raw_path is the verbatim request-target: "/path" for origin-form
        # requests, the full absolute URI for proxy (absolute-form) requests.
        raw_target = request.raw_path
        if raw_target.startswith("/"):
            if raw_target == "/healthz":
                return web.Response(text="ok\n")
            return web.Response(
                status=400, text="This is a forward HTTP proxy; set http_proxy and request http:// URLs.\n"
            )
        target = URL(raw_target, encoded=True)
        if target.scheme != "http" or target.host is None:
            return web.Response(status=400, text=f"Unsupported proxy target {raw_target!r}; use http:// URLs.\n")
        try:
            return await self._serve(target)
        except NoCaptureError as e:
            return web.Response(status=404, text=f"{e}\n")
        except ClampViolationError as e:
            return web.Response(status=403, text=f"{e}\n")
        except UpstreamError as e:
            logger.warning("upstream error for %s: %s", target, e)
            return web.Response(status=502, text=f"{e}\n")

    async def _serve(self, target: URL) -> web.Response:
        if target.host in (ARCHIVE_HOST, self._config.upstream_host):
            return await self._serve_archive_url(target)
        ts, original = await self._resolve_capture(target)
        snapshot = await self._fetch_capture(ts, "id_", original)
        return self._respond(str(target), snapshot)

    async def _serve_archive_url(self, target: URL) -> web.Response:
        """Defense in depth for explicitly archive-addressed requests.

        Pinned ``/web/<ts>/`` URLs (e.g. curated Task.evidence) are served iff
        ts ≤ as_of; CDX queries are forwarded with ``to`` clamped so the agent
        cannot even learn that a post-as_of capture exists.
        """
        path_qs = target.raw_path_qs
        if (parsed := parse_web_path(path_qs)) is not None:
            ts, modifier, inner = parsed
            if not ts_allowed(ts, self._config.as_of_ts):
                raise ClampViolationError(f"capture {ts} is after as_of {self._config.as_of}")
            snapshot = await self._fetch_capture(ts, modifier or "id_", inner)
            return self._respond(inner, snapshot)
        if path_qs.startswith(CDX_PATH):
            return await self._forward_cdx(target)
        raise ClampViolationError(f"only /web/ and {CDX_PATH} paths are allowed on the archive host")

    async def _forward_cdx(self, target: URL) -> web.Response:
        params = dict(target.query)
        # A partial `to` means "through the end of that period" — pad with 9s
        # before comparing so the clamp never *widens* the requested bound.
        requested_to = params.get("to")
        params["to"] = (
            min(requested_to.ljust(14, "9"), self._config.as_of_ts) if requested_to else self._config.as_of_ts
        )
        async with self._session.get(URL(self._config.upstream + CDX_PATH), params=params) as response:
            body = await response.content.read(MAX_BODY_BYTES)
            if response.status != 200:
                raise UpstreamError(f"CDX query failed with HTTP {response.status}")
            return web.Response(body=body, headers={"Content-Type": response.headers.get("Content-Type", "text/plain")})

    async def _resolve_capture(self, target: URL) -> tuple[str, str]:
        """Newest (timestamp, original URL) capture of `target` at or before as_of."""
        params = {"url": str(target), "to": self._config.as_of_ts, "output": "json", "limit": "-1"}
        async with self._session.get(URL(self._config.upstream + CDX_PATH), params=params) as response:
            if response.status != 200:
                raise UpstreamError(f"CDX lookup failed with HTTP {response.status}")
            raw = await response.content.read(MAX_BODY_BYTES)
        if not raw.strip():
            raise NoCaptureError(f"no archived capture of {target} at or before {self._config.as_of}")
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError as e:
            raise UpstreamError(f"CDX returned invalid JSON: {e}") from e
        if not isinstance(rows, list):
            raise UpstreamError(f"CDX returned non-list JSON: {type(rows).__name__}")
        try:
            picked = pick_capture(rows)
        except (ValueError, IndexError) as e:
            raise UpstreamError(f"CDX rows have unexpected shape: {e}") from e
        if picked is None:
            raise NoCaptureError(f"no archived capture of {target} at or before {self._config.as_of}")
        return picked

    async def _fetch_capture(self, ts: str, modifier: str, url: str) -> Snapshot:
        """Fetch /web/<ts><modifier>/<url> from upstream, following archive-internal redirects.

        Every hop that stays on the archive is re-clamped (IA canonicalizes
        partial/inexact timestamps to the *closest* capture, which can walk
        forward past as_of). Off-archive Locations (captured live-web
        redirects) are bounced back to the client so the follow-up request
        re-enters the proxy and is re-resolved under the clamp.
        """
        current = URL(f"{self._config.upstream}/web/{ts}{modifier}/{url}", encoded=True)
        final_ts = ts
        for _ in range(MAX_REDIRECT_HOPS):
            async with self._session.get(
                current, allow_redirects=False, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if location is None:
                        raise UpstreamError(f"redirect without Location from {current}")
                    next_url = current.join(URL(location, encoded=True))
                    on_archive = next_url.host in (ARCHIVE_HOST, self._config.upstream_host)
                    if on_archive and (parsed := parse_web_path(next_url.raw_path_qs)) is not None:
                        hop_ts, _, _ = parsed
                        if not ts_allowed(hop_ts, self._config.as_of_ts):
                            raise ClampViolationError(
                                f"archive redirected to capture {hop_ts}, after as_of {self._config.as_of}"
                            )
                        final_ts = hop_ts
                        # Re-anchor on the configured upstream so redirect
                        # targets are also fetched through the shared cache.
                        current = URL(self._config.upstream + next_url.raw_path_qs, encoded=True)
                        continue
                    # Captured live-web redirect: hand it to the client,
                    # downgraded to http so it can re-enter this proxy.
                    bounce = next_url.with_scheme("http") if next_url.scheme == "https" else next_url
                    return Snapshot(
                        ts=final_ts, status=response.status, content_type="", body=b"", location=str(bounce)
                    )
                body = await response.content.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise UpstreamError(f"capture body exceeds {MAX_BODY_BYTES} bytes")
                # Replayed captures carry Memento-Datetime — even archived
                # 404s/500s (which are correct as-of content). Error statuses
                # without it are the archive/cache itself failing.
                if response.status >= 400 and "Memento-Datetime" not in response.headers:
                    raise UpstreamError(f"archive upstream returned HTTP {response.status} for {current}")
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                return Snapshot(ts=final_ts, status=response.status, content_type=content_type, body=body)
        raise UpstreamError(f"redirect chain exceeded {MAX_REDIRECT_HOPS} hops for {url}")

    def _respond(self, served_url: str, snapshot: Snapshot) -> web.Response:
        headers = {"X-Wayback-Timestamp": snapshot.ts}
        if snapshot.location is not None:
            headers["Location"] = snapshot.location
            return web.Response(status=snapshot.status, headers=headers)
        self._emit_manifest(served_url, snapshot)
        headers["Content-Type"] = snapshot.content_type
        return web.Response(status=snapshot.status, body=snapshot.body, headers=headers)

    def _emit_manifest(self, url: str, snapshot: Snapshot) -> None:
        """Served-evidence record; the harness attaches these to the run payload (W3)."""
        line = json.dumps(
            {
                "url": url,
                "capture_ts": snapshot.ts,
                "sha256": hashlib.sha256(snapshot.body).hexdigest(),
                "size": len(snapshot.body),
            }
        )
        print(line, file=self._manifest, flush=True)


async def start_proxy(
    config: Config, session: aiohttp.ClientSession, manifest: TextIO, host: str = "0.0.0.0"
) -> web.ServerRunner:
    """Start the proxy; returns the runner (bound port in runner.addresses)."""
    proxy = WaybackProxy(config, session, manifest)
    runner = web.ServerRunner(web.Server(proxy.handle))
    await runner.setup()
    await web.TCPSite(runner, host, config.port).start()
    return runner


async def amain(config: Config, manifest: TextIO) -> None:
    logger.info("wayback proxy: as_of=%s upstream=%s port=%d", config.as_of, config.upstream, config.port)
    # Authorization rides on every request; the proxy only ever GETs `upstream`
    # (off-archive redirects are returned to the client, never followed).
    headers = {"Authorization": config.upstream_auth} if config.upstream_auth is not None else {}
    # Total timeout rides out wayback-cache limit_req delays (burst 60 @ 30r/m).
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300), headers=headers) as session:
        await start_proxy(config, session, manifest)
        await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    # The manifest file opens in sync main: blocking open in async code is an
    # antipattern, and line buffering keeps records readable mid-run.
    manifest_cm = (
        nullcontext(sys.stdout) if config.manifest_path is None else config.manifest_path.open("a", buffering=1)
    )
    with manifest_cm as manifest:
        asyncio.run(amain(config, manifest))


if __name__ == "__main__":
    main()
