"""Tests for the date-clamping wayback proxy against the canned fake IA.

Behavioral cases drive the mitmproxy :class:`WaybackAddon` directly — synthesize
an ``http.HTTPFlow``, run the request hook, inspect the response it set — which
exercises the full path (resolver + clamp + exception mapping) without standing
up a TLS-intercepting listener. The pure helpers are unit-tested at the bottom.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date

import aiohttp
import pytest
import pytest_bazel
from mitmproxy.http import Headers
from mitmproxy.test import tflow
from yarl import URL

from loom.wayback_proxy import fake_ia
from loom.wayback_proxy.addon import HEALTH_HOST, WaybackAddon
from loom.wayback_proxy.proxy import (
    Config,
    UpstreamError,
    WaybackResolver,
    parse_web_path,
    pick_available_capture,
    pick_capture,
    ts_allowed,
)

AS_OF_TS = "20200601235959"


@dataclass(frozen=True)
class FetchResult:
    status: int
    headers: Headers  # mitmproxy Headers: case-insensitive membership and lookup
    body: bytes


Fetch = Callable[[str], Awaitable[FetchResult]]


@pytest.fixture
async def fake_upstream() -> AsyncIterator[str]:
    runner = await fake_ia.start()
    yield f"http://127.0.0.1:{int(runner.addresses[0][1])}"
    await runner.cleanup()


@pytest.fixture
async def manifest() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
async def addon(fake_upstream: str, manifest: io.StringIO) -> AsyncIterator[WaybackAddon]:
    config = Config(as_of=fake_ia.AS_OF, upstream=fake_upstream, port=0)
    async with aiohttp.ClientSession() as session:
        yield WaybackAddon(WaybackResolver(config, session, manifest))


@pytest.fixture
def fetch(addon: WaybackAddon) -> Fetch:
    async def fetch_via_addon(url: str) -> FetchResult:
        # Set request components from the URL directly (rather than the
        # round-trip-lossy Request.url setter) so nested archive paths like
        # /web/<ts>/https://… survive verbatim into the addon.
        parsed = URL(url, encoded=True)
        assert parsed.host is not None
        flow = tflow.tflow()
        flow.response = None
        flow.request.scheme = parsed.scheme
        flow.request.host = parsed.host
        flow.request.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        flow.request.path = parsed.raw_path_qs
        flow.request.headers["host"] = parsed.host
        await addon.request(flow)
        response = flow.response
        assert response is not None, "addon must set flow.response for every request"
        return FetchResult(status=response.status_code, headers=response.headers, body=response.content)

    return fetch_via_addon


async def test_serves_newest_capture_at_or_before_as_of(fetch: Fetch) -> None:
    result = await fetch(fake_ia.EXAMPLE_URL)
    assert result.status == 200
    assert result.body == fake_ia.EXAMPLE_BODY
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS
    assert result.headers["Content-Type"] == "text/html"


async def test_https_request_is_served_without_rewriting_to_http(fetch: Fetch) -> None:
    # The headline of W4: an agent issues the natural https:// URL and gets the
    # same clamped capture — no http:// downgrade, no URL surgery.
    result = await fetch("https://example.com/")
    assert result.status == 200
    assert result.body == fake_ia.EXAMPLE_BODY
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS


async def test_normal_lookup_uses_availability_not_cdx(fetch: Fetch) -> None:
    result = await fetch(fake_ia.CDX_FAILS_BUT_AVAILABLE_URL)
    assert result.status == 200
    assert result.body == fake_ia.CDX_FAILS_BUT_AVAILABLE_BODY
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS


async def test_manifest_records_served_evidence(fetch: Fetch, manifest: io.StringIO) -> None:
    await fetch(fake_ia.EXAMPLE_URL)
    record = json.loads(manifest.getvalue())
    assert record == {
        "kind": "served",
        "url": fake_ia.EXAMPLE_URL,
        "capture_ts": fake_ia.GOOD_TS,
        "sha256": hashlib.sha256(fake_ia.EXAMPLE_BODY).hexdigest(),
        "size": len(fake_ia.EXAMPLE_BODY),
    }


async def test_no_capture_at_or_before_as_of_is_404(fetch: Fetch) -> None:
    result = await fetch(fake_ia.FUTURE_ONLY_URL)
    assert result.status == 404
    assert str(fake_ia.AS_OF) in result.body.decode()
    assert "X-Wayback-Timestamp" not in result.headers


async def test_follows_timestamp_canonicalization_redirect(fetch: Fetch) -> None:
    result = await fetch(fake_ia.CANON_URL)
    assert result.status == 200
    assert result.body == fake_ia.CANON_BODY
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS


async def test_refuses_redirect_drifting_past_as_of(fetch: Fetch) -> None:
    result = await fetch(fake_ia.DRIFT_URL)
    assert result.status == 403
    assert fake_ia.TOO_NEW_TS in result.body.decode()


async def test_bounces_captured_live_redirect_preserving_scheme(fetch: Fetch) -> None:
    result = await fetch(fake_ia.MOVED_URL)
    assert result.status == 302
    # Location is handed back unchanged; the client's follow-up CONNECTs to the
    # https target and re-enters the proxy (MITM means no http downgrade needed).
    assert result.headers["Location"] == fake_ia.MOVED_TARGET


async def test_archived_404_is_served_as_of_content(fetch: Fetch) -> None:
    result = await fetch(fake_ia.GONE_URL)
    assert result.status == 404
    assert result.body == fake_ia.GONE_BODY
    # Distinguishable from the proxy's own "no capture" 404 by the ts header.
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS


async def test_cdx_failure_maps_to_502(fetch: Fetch) -> None:
    result = await fetch(fake_ia.CDX_BROKEN_URL)
    assert result.status == 502


async def test_upstream_error_body_recorded_to_manifest(fetch: Fetch, manifest: io.StringIO) -> None:
    # A CDX 503 from IA must be captured (status + body) for diagnosis, not
    # swallowed into an opaque 502 — the body distinguishes a bad gateway from
    # a rate-limit notice when a degraded archive run is post-mortemed.
    await fetch(fake_ia.CDX_BROKEN_URL)
    record = json.loads(manifest.getvalue())
    assert record["kind"] == "upstream_error"
    assert record["status"] == 503
    assert record["body"] == "CDX is having a bad day\n"


async def test_explicit_pinned_capture_within_as_of_served(fetch: Fetch) -> None:
    result = await fetch(f"http://web.archive.org/web/{fake_ia.GOOD_TS}id_/{fake_ia.EXAMPLE_ORIGINAL}")
    assert result.status == 200
    assert result.body == fake_ia.EXAMPLE_BODY


async def test_explicit_capture_after_as_of_refused(fetch: Fetch) -> None:
    result = await fetch(f"http://web.archive.org/web/{fake_ia.TOO_NEW_TS}id_/{fake_ia.EXAMPLE_ORIGINAL}")
    assert result.status == 403


async def test_cdx_passthrough_clamps_to_param(fetch: Fetch) -> None:
    result = await fetch(f"http://web.archive.org{fake_ia.CDX_PATH}?url={fake_ia.EXAMPLE_URL}&output=json")
    assert result.status == 200
    listed_timestamps = {row[1] for row in json.loads(result.body)[1:]}
    assert fake_ia.GOOD_TS in listed_timestamps
    assert fake_ia.TOO_NEW_TS not in listed_timestamps


async def test_other_archive_paths_refused(fetch: Fetch) -> None:
    result = await fetch("http://web.archive.org/somewhere-else")
    assert result.status == 403


async def test_health_host_answered_without_touching_archive(fetch: Fetch, manifest: io.StringIO) -> None:
    result = await fetch(f"http://{HEALTH_HOST}/healthz")
    assert result.status == 200
    assert result.body == b"ok\n"
    # The health probe is not archive evidence.
    assert manifest.getvalue() == ""


def test_ts_allowed_boundaries() -> None:
    assert ts_allowed(AS_OF_TS, AS_OF_TS)
    assert ts_allowed("20200601235958", AS_OF_TS)
    assert not ts_allowed("20200602000000", AS_OF_TS)
    # Partial timestamps are zero-padded (earliest moment of the period).
    assert ts_allowed("2020", AS_OF_TS)
    assert not ts_allowed("202012", AS_OF_TS)


def test_parse_web_path() -> None:
    assert parse_web_path("/web/20200115103000id_/http://example.com/a?q=1") == (
        "20200115103000",
        "id_",
        "http://example.com/a?q=1",
    )
    assert parse_web_path("/web/2020/http://example.com/") == ("2020", "", "http://example.com/")
    assert parse_web_path("/web/20200115103000id_") is None
    assert parse_web_path("/cdx/search/cdx?url=x") is None


def test_pick_capture() -> None:
    assert pick_capture([]) is None
    assert pick_capture([["urlkey", "timestamp", "original"]]) is None
    # Columns are resolved by header name, not position.
    rows = [
        ["original", "timestamp", "urlkey"],
        ["https://a/", "20190101000000", "key"],
        ["https://b/", "20200101000000", "key"],
    ]
    assert pick_capture(rows) == ("20200101000000", "https://b/")


def test_pick_available_capture() -> None:
    payload = {
        "archived_snapshots": {
            "closest": {
                "status": "200",
                "available": True,
                "url": "http://web.archive.org/web/20200101000000/https://example.com/?q=1",
                "timestamp": "20200101000000",
            }
        }
    }
    assert pick_available_capture(payload, AS_OF_TS) == ("20200101000000", "https://example.com/?q=1")


def test_pick_available_capture_rejects_future_closest() -> None:
    payload = {
        "archived_snapshots": {
            "closest": {
                "status": "200",
                "available": True,
                "url": f"http://web.archive.org/web/{fake_ia.TOO_NEW_TS}/https://example.com/",
                "timestamp": fake_ia.TOO_NEW_TS,
            }
        }
    }
    assert pick_available_capture(payload, AS_OF_TS) is None


def test_pick_available_capture_ignores_unavailable() -> None:
    assert pick_available_capture({"archived_snapshots": {}}, AS_OF_TS) is None
    payload = {
        "archived_snapshots": {
            "closest": {
                "status": "404",
                "available": False,
                "url": "http://web.archive.org/web/20200101000000/https://example.com/",
                "timestamp": "20200101000000",
            }
        }
    }
    assert pick_available_capture(payload, AS_OF_TS) is None


def test_pick_available_capture_rejects_malformed_shape() -> None:
    payload = {
        "archived_snapshots": {
            "closest": {
                "status": "200",
                "available": True,
                "url": "http://web.archive.org/web/20200101000000/https://example.com/",
                "timestamp": "not-a-timestamp",
            }
        }
    }
    with pytest.raises(UpstreamError, match="Availability response shape"):
        pick_available_capture(payload, AS_OF_TS)


def test_config_requires_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYBACK_AS_OF", raising=False)
    with pytest.raises(RuntimeError, match="WAYBACK_AS_OF"):
        Config.from_env()


def test_config_as_of_ts() -> None:
    config = Config(as_of=date(2020, 6, 1), upstream="https://web.archive.org", port=8080)
    assert config.as_of_ts == AS_OF_TS


def test_config_uses_archive_org_for_direct_availability() -> None:
    config = Config(as_of=date(2020, 6, 1), upstream="https://web.archive.org", port=8080)
    assert config.availability_base == "https://archive.org"


def test_config_from_env_uses_cache_for_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYBACK_AS_OF", "2020-06-01")
    monkeypatch.setenv("WAYBACK_UPSTREAM", "http://wayback-cache.local:8080")
    monkeypatch.delenv("WAYBACK_AVAILABILITY_UPSTREAM", raising=False)
    config = Config.from_env()
    assert config.upstream == "http://wayback-cache.local:8080"
    assert config.availability_base == "http://wayback-cache.local:8080"


if __name__ == "__main__":
    pytest_bazel.main()
