# mitmproxy addon: URL-keyed cache for immutable build-dependency downloads.
#
# WHY an addon (not a Squid/nginx sidecar): haku-mitmproxy is the TLS-terminating
# hop. HTTPS reaches it as CONNECT tunnels that a plain caching proxy can only
# blindly forward; only an in-mitmproxy addon sees the decrypted GET/response and
# can cache it. This lets the haku-ci Bazel/toolchain cold-fetch path (no RBE) hit
# a local cache instead of re-downloading every artifact.
#
# SAFETY — default-deny host allowlist. Only GET responses from the immutable
# public-artifact hosts in CACHEABLE_HOSTS are ever written to or served from the
# cache. Haku's credential-injected / API hosts (googleapis, gmail, tasks,
# anthropic, haku-mailbox.allegedly.works, ...) are absent by construction, so no
# credentialed response can ever enter the cache. This is an allowlist, never a
# denylist: an unknown host is not cached. Integrity of served artifacts is
# checked downstream (Bazel/npm/pip verify checksums), so URL-keyed replay of
# immutable artifacts is safe; we still refuse to cache anything that smells
# dynamic (non-200, ranges, Set-Cookie, no-store/private, credentialed requests).
#
# api.anthropic.com never reaches this addon at all: the deployment passes it via
# --ignore-hosts (raw TCP passthrough), so its flows are never parsed as HTTP.

import hashlib
import json
import logging
import os
from pathlib import Path

from mitmproxy import http

logger = logging.getLogger(__name__)

# STRICT allowlist of immutable build-dependency hosts. Extend deliberately; every
# addition must be a host that serves content-addressed / versioned artifacts with
# no per-user or credentialed variation.
CACHEABLE_HOSTS = frozenset(
    {
        "bcr.bazel.build",
        "releases.bazel.build",
        "github.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "raw.githubusercontent.com",
        "nodejs.org",
        "registry.npmjs.org",
        "pypi.org",
        "files.pythonhosted.org",
        "cache.nixos.org",
    }
)

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
# Per-object cap: skip caching a single response larger than this (bounds one
# pathological artifact from filling the volume).
MAX_OBJECT_BYTES = int(os.environ.get("CACHE_MAX_OBJECT_BYTES", str(512 * 1024 * 1024)))
# Total-volume cap: evict oldest entries (by mtime) to make room before writing.
MAX_TOTAL_BYTES = int(os.environ.get("CACHE_MAX_TOTAL_BYTES", str(20 * 1024 * 1024 * 1024)))

# Response headers we must not replay verbatim: the cached body is already decoded
# and Response.make recomputes framing, so replaying these corrupts the response.
_STRIPPED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
# Request headers whose presence means the exchange may be credentialed/personalized;
# such a request neither reads nor writes the shared cache.
_CREDENTIAL_HEADERS = ("authorization", "cookie", "proxy-authorization")


class BuildDepCache:
    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def _meta_path(self, key: str) -> Path:
        return CACHE_DIR / f"{key}.meta"

    def _body_path(self, key: str) -> Path:
        return CACHE_DIR / f"{key}.body"

    def _cacheable_request(self, request: http.Request) -> bool:
        return (
            request.method == "GET"
            and request.pretty_host in CACHEABLE_HOSTS
            and "range" not in request.headers
            and not any(h in request.headers for h in _CREDENTIAL_HEADERS)
        )

    def request(self, flow: http.HTTPFlow) -> None:
        # Serve a hit without touching the origin.
        if not self._cacheable_request(flow.request):
            return
        key = self._key(flow.request.pretty_url)
        meta_path, body_path = self._meta_path(key), self._body_path(key)
        if not (meta_path.exists() and body_path.exists()):
            return
        try:
            meta = json.loads(meta_path.read_text())
            body = body_path.read_bytes()
        except OSError as exc:
            # A cache read failure must degrade to a normal origin fetch, never
            # break the request. (Genuinely handled: fall through to the origin.)
            logger.warning("cache read failed for %s: %s", flow.request.pretty_url, exc)
            return
        headers = dict(meta["headers"])
        headers["X-Haku-Cache"] = "HIT"
        flow.response = http.Response.make(200, body, headers)
        logger.info("cache HIT %s (%d bytes)", flow.request.pretty_url, len(body))

    def response(self, flow: http.HTTPFlow) -> None:
        # Populate the cache from a fresh, cacheable origin response. This hook
        # also fires for a response we synthesized in request(); the X-Haku-Cache
        # marker lets us skip re-storing our own replayed hit.
        if flow.response is None or flow.response.headers.get("X-Haku-Cache") == "HIT":
            return
        if not self._cacheable_request(flow.request):
            return
        response = flow.response
        cache_control = response.headers.get("cache-control", "").lower()
        if (
            response.status_code != 200
            or "content-range" in response.headers
            or "set-cookie" in response.headers
            or "no-store" in cache_control
            or "private" in cache_control
        ):
            return
        body = response.content
        if body is None or len(body) > MAX_OBJECT_BYTES:
            return
        headers = [
            (name, value) for name, value in response.headers.items(multi=True) if name.lower() not in _STRIPPED_HEADERS
        ]
        self._store(flow.request.pretty_url, headers, body)

    def _store(self, url: str, headers: list[tuple[str, str]], body: bytes) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._evict_for(len(body))
            key = self._key(url)
            # Write body to a temp file then rename so a concurrent reader never
            # sees a partial body next to a committed meta.
            tmp = self._body_path(key).with_suffix(".body.tmp")
            tmp.write_bytes(body)
            tmp.rename(self._body_path(key))
            self._meta_path(key).write_text(json.dumps({"url": url, "headers": headers}))
            logger.info("cache STORE %s (%d bytes)", url, len(body))
        except OSError as exc:
            # A write failure must not break the proxied response the client is
            # already receiving; the origin fetch succeeded. Log and move on.
            logger.warning("cache store failed for %s: %s", url, exc)

    def _evict_for(self, incoming: int) -> None:
        bodies = sorted(CACHE_DIR.glob("*.body"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in bodies)
        while bodies and total + incoming > MAX_TOTAL_BYTES:
            victim = bodies.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
            victim.with_name(victim.stem + ".meta").unlink(missing_ok=True)
            logger.info("cache EVICT %s", victim.name)


addons = [BuildDepCache()]
