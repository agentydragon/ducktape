"""Date-clamping Wayback Machine capture resolver (the loom "time machine").

Resolves every requested URL to the newest Internet Archive capture
at-or-before ``WAYBACK_AS_OF``, served as raw ``id_`` bytes (no IA banner or
rewriting). This module is the framework-neutral core: it takes a target
``URL`` and returns a :class:`ProxyResponse` (or raises one of the typed
errors below). The mitmproxy wiring that turns it into a forward/MITM proxy
lives in ``addon.py`` / ``server.py``. See loom/plans/wayback_proxy.md.

Configuration (env, parsed by :class:`Config`): ``WAYBACK_AS_OF`` (required ISO
date, inclusive), ``WAYBACK_UPSTREAM`` (default ``https://web.archive.org``;
point at the wayback-cache cluster service to share a pull-through cache),
``WAYBACK_AVAILABILITY_UPSTREAM`` (defaults to ``https://archive.org`` for
direct-IA mode, or ``WAYBACK_UPSTREAM`` when a cache is configured),
``WAYBACK_UPSTREAM_AUTH`` (``Authorization`` header for the authed cache route),
``WAYBACK_MANIFEST_PATH`` (write the manifest to this file instead of stdout -
the gym scorer reads it out of the sandbox per sample).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yarl import URL

logger = logging.getLogger(__name__)

CDX_PATH = "/cdx/search/cdx"
ARCHIVE_HOST = "web.archive.org"
AVAILABILITY_HOST = "archive.org"
AVAILABILITY_PATH = "/wayback/available"
MAX_REDIRECT_HOPS = 10
MAX_BODY_BYTES = 64 * 2**20
# Upstream error bodies (IA/cache 5xx pages) are captured to the manifest for
# diagnosis; a few KB of the HTML is enough to tell a bad-gateway page from a
# rate-limit notice without bloating the per-sample evidence.
MAX_ERROR_BODY_BYTES = 4096

# "/web/<ts><modifier?>/<url>" — IA replay path. Modifiers are two letters
# plus underscore (id_, if_, im_, js_, cs_, ...).
_WEB_PATH_RE = re.compile(r"^/web/(\d{4,14})([a-z]{2}_)?/(.*)$")
_IA_SIGNAL_HEADERS = ("retry-after", "x-rl", "x-na", "x-app-server", "x-ts", "x-tr")


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


def _truthy_availability(value: object) -> bool:
    return value is True or value in {"true", "True", "1", 1}


class AvailabilityClosest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    available: object = False
    timestamp: str
    url: str

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        if re.fullmatch(r"\d{4,14}", value) is None:
            raise ValueError("must be a 4-14 digit IA timestamp")
        return value


class ArchivedSnapshots(BaseModel):
    model_config = ConfigDict(extra="ignore")

    closest: AvailabilityClosest | None = None


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    archived_snapshots: ArchivedSnapshots = Field(default_factory=ArchivedSnapshots)


def pick_available_capture(payload: object, as_of_ts: str) -> tuple[str, str] | None:
    """Return (timestamp, original URL) from a Wayback Availability response.

    ``None`` means the response is well-formed but not sufficient for the proxy's
    strict as-of semantics (no closest capture, unavailable, or closest is after
    the clamp). The caller can then fall back to CDX. Malformed responses raise
    :class:`UpstreamError`.
    """
    try:
        response = AvailabilityResponse.model_validate(payload)
    except ValidationError as e:
        raise UpstreamError(f"Availability response shape is unexpected: {e}") from e

    closest = response.archived_snapshots.closest
    if closest is None:
        return None
    if not _truthy_availability(closest.available):
        return None
    timestamp = closest.timestamp
    replay_url_raw = closest.url
    try:
        replay_url = URL(replay_url_raw, encoded=True)
    except ValueError as e:
        raise UpstreamError(f"Availability replay url is invalid: {replay_url_raw!r}") from e
    parsed = parse_web_path(replay_url.raw_path_qs)
    if parsed is None:
        raise UpstreamError(f"Availability replay url is not a Wayback replay path: {replay_url_raw!r}")
    replay_ts, _modifier, original = parsed
    if timestamp != replay_ts:
        raise UpstreamError(f"Availability timestamp {timestamp} does not match replay URL timestamp {replay_ts}")
    if not ts_allowed(timestamp, as_of_ts):
        return None
    return timestamp, original


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
    # Base URL for /wayback/available. If None, direct web.archive.org upstreams
    # use archive.org; custom/cache upstreams use the same base as `upstream`.
    availability_upstream: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        as_of_raw = os.environ.get("WAYBACK_AS_OF")
        if as_of_raw is None:
            raise RuntimeError("WAYBACK_AS_OF must be set to the ISO information-cutoff date")
        upstream_env = os.environ.get("WAYBACK_UPSTREAM")
        upstream = (upstream_env or f"https://{ARCHIVE_HOST}").rstrip("/")
        availability_env = os.environ.get("WAYBACK_AVAILABILITY_UPSTREAM")
        manifest_raw = os.environ.get("WAYBACK_MANIFEST_PATH")
        return cls(
            as_of=date.fromisoformat(as_of_raw),
            upstream=upstream,
            port=int(os.environ.get("PORT", "8080")),
            manifest_path=Path(manifest_raw) if manifest_raw is not None else None,
            # Empty (the compose default when no auth) is treated as unset.
            upstream_auth=os.environ.get("WAYBACK_UPSTREAM_AUTH") or None,
            availability_upstream=(
                availability_env.rstrip("/")
                if availability_env
                else (upstream if upstream_env else f"https://{AVAILABILITY_HOST}")
            ),
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

    @property
    def availability_base(self) -> str:
        if self.availability_upstream is not None:
            return self.availability_upstream.rstrip("/")
        if self.upstream_host == ARCHIVE_HOST:
            return f"https://{AVAILABILITY_HOST}"
        return self.upstream


@dataclass(frozen=True)
class Snapshot:
    """One archive response: either a body to serve or an off-archive redirect."""

    ts: str
    status: int
    content_type: str
    body: bytes
    location: str | None = None


@dataclass(frozen=True)
class ProxyResponse:
    """Framework-neutral proxy result, mapped to an HTTP response by the addon."""

    status: int
    headers: dict[str, str]
    body: bytes


class WaybackResolver:
    """Turns a requested URL into the clamped archive capture that answers it.

    Scheme-agnostic: the caller passes the requested ``URL`` (``http`` or the
    MITM-intercepted ``https``) and IA serves the same snapshot regardless of
    the original scheme. Emits a served-evidence JSONL manifest line per body
    response on ``manifest`` (the harness attaches these to the run payload).
    """

    def __init__(self, config: Config, session: aiohttp.ClientSession, manifest: TextIO) -> None:
        self._config = config
        self._session = session
        self._manifest = manifest

    async def serve(self, target: URL) -> ProxyResponse:
        if target.host in (ARCHIVE_HOST, self._config.upstream_host):
            return await self._serve_archive_url(target)
        ts, original = await self._resolve_capture(target)
        snapshot = await self._fetch_capture(ts, "id_", original)
        return self._respond(str(target), snapshot)

    async def _serve_archive_url(self, target: URL) -> ProxyResponse:
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

    async def _forward_cdx(self, target: URL) -> ProxyResponse:
        params = dict(target.query)
        # A partial `to` means "through the end of that period" — pad with 9s
        # before comparing so the clamp never *widens* the requested bound.
        requested_to = params.get("to")
        params["to"] = (
            min(requested_to.ljust(14, "9"), self._config.as_of_ts) if requested_to else self._config.as_of_ts
        )
        async with self._session.get(
            URL(self._config.upstream + CDX_PATH), params=params, headers=self._auth_headers_for(self._config.upstream)
        ) as response:
            body = await response.content.read(MAX_BODY_BYTES)
            if response.status != 200:
                self._emit_upstream_error(str(response.url), response.status, body, response.headers)
                raise UpstreamError(f"CDX query failed with HTTP {response.status}")
            return ProxyResponse(
                status=200, headers={"Content-Type": response.headers.get("Content-Type", "text/plain")}, body=body
            )

    async def _resolve_capture(self, target: URL) -> tuple[str, str]:
        """Newest (timestamp, original URL) capture of `target` at or before as_of."""
        if (picked := await self._resolve_capture_via_availability(target)) is not None:
            return picked
        return await self._resolve_capture_via_cdx(target)

    async def _resolve_capture_via_availability(self, target: URL) -> tuple[str, str] | None:
        params = {"url": str(target), "timestamp": self._config.as_of_ts}
        availability_base = self._config.availability_base
        async with self._session.get(
            URL(availability_base + AVAILABILITY_PATH), params=params, headers=self._auth_headers_for(availability_base)
        ) as response:
            if response.status != 200:
                body = await response.content.read(MAX_ERROR_BODY_BYTES)
                self._emit_upstream_error(str(response.url), response.status, body, response.headers)
                raise UpstreamError(f"Availability lookup failed with HTTP {response.status}")
            raw = await response.content.read(MAX_BODY_BYTES)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise UpstreamError(f"Availability returned invalid JSON: {e}") from e
        return pick_available_capture(payload, self._config.as_of_ts)

    async def _resolve_capture_via_cdx(self, target: URL) -> tuple[str, str]:
        """CDX fallback for unavailable/future Availability answers and archived non-200 captures."""
        params = {"url": str(target), "to": self._config.as_of_ts, "output": "json", "limit": "-1"}
        async with self._session.get(
            URL(self._config.upstream + CDX_PATH), params=params, headers=self._auth_headers_for(self._config.upstream)
        ) as response:
            if response.status != 200:
                self._emit_upstream_error(
                    str(response.url),
                    response.status,
                    await response.content.read(MAX_ERROR_BODY_BYTES),
                    response.headers,
                )
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
            headers = {"Accept-Encoding": "identity", **self._auth_headers_for(self._config.upstream)}
            async with self._session.get(current, allow_redirects=False, headers=headers) as response:
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
                    # Captured live-web redirect: hand it to the client as-is so
                    # the follow-up request re-enters this proxy. HTTPS stays
                    # https now that the proxy MITMs TLS — no http downgrade.
                    return Snapshot(
                        ts=final_ts, status=response.status, content_type="", body=b"", location=str(next_url)
                    )
                body = await response.content.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise UpstreamError(f"capture body exceeds {MAX_BODY_BYTES} bytes")
                # Replayed captures carry Memento-Datetime — even archived
                # 404s/500s (which are correct as-of content). Error statuses
                # without it are the archive/cache itself failing.
                if response.status >= 400 and "Memento-Datetime" not in response.headers:
                    self._emit_upstream_error(str(response.url), response.status, body, response.headers)
                    raise UpstreamError(f"archive upstream returned HTTP {response.status} for {current}")
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                return Snapshot(ts=final_ts, status=response.status, content_type=content_type, body=body)
        raise UpstreamError(f"redirect chain exceeded {MAX_REDIRECT_HOPS} hops for {url}")

    def _respond(self, served_url: str, snapshot: Snapshot) -> ProxyResponse:
        headers = {"X-Wayback-Timestamp": snapshot.ts}
        if snapshot.location is not None:
            headers["Location"] = snapshot.location
            return ProxyResponse(status=snapshot.status, headers=headers, body=b"")
        self._emit_manifest(served_url, snapshot)
        headers["Content-Type"] = snapshot.content_type
        return ProxyResponse(status=snapshot.status, headers=headers, body=snapshot.body)

    def _emit_manifest(self, url: str, snapshot: Snapshot) -> None:
        """Served-evidence record; the harness attaches these to the run payload (W3)."""
        line = json.dumps(
            {
                "kind": "served",
                "url": url,
                "capture_ts": snapshot.ts,
                "sha256": hashlib.sha256(snapshot.body).hexdigest(),
                "size": len(snapshot.body),
            }
        )
        print(line, file=self._manifest, flush=True)

    def _auth_headers_for(self, upstream_base: str) -> dict[str, str]:
        if self._config.upstream_auth is not None and upstream_base.rstrip("/") == self._config.upstream:
            return {"Authorization": self._config.upstream_auth}
        return {}

    def _emit_upstream_error(self, request_url: str, status: int, body: bytes, headers: object | None = None) -> None:
        """Upstream-failure record: archive/cache returned HTTP ≥400 unexpectedly
        (an IA bad gateway or a rate-limit notice, not an archived error page).

        Shares the manifest with served records, tagged by `kind`, so the harness
        surfaces a degraded run's failures per sample instead of swallowing the
        body into an opaque 502."""
        record = {
            "kind": "upstream_error",
            "request_url": request_url,
            "status": status,
            "body": body[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace"),
        }
        if headers is not None:
            signal_headers = {
                name: value for name in _IA_SIGNAL_HEADERS if (value := headers.get(name, None)) is not None
            }
            if signal_headers:
                record["headers"] = signal_headers
        line = json.dumps(record)
        print(line, file=self._manifest, flush=True)
