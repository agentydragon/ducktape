"""Tests for the date-clamping wayback proxy against the canned fake IA."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web

from loom.wayback_proxy import fake_ia
from loom.wayback_proxy.proxy import Config, parse_web_path, pick_capture, start_proxy, ts_allowed

AS_OF_TS = "20200601235959"


def _port(runner: web.ServerRunner) -> int:
    return int(runner.addresses[0][1])


@dataclass(frozen=True)
class RunningProxy:
    url: str
    manifest: io.StringIO


@dataclass(frozen=True)
class FetchResult:
    status: int
    headers: dict[str, str]
    body: bytes


@pytest.fixture
async def fake_upstream() -> AsyncIterator[str]:
    runner = await fake_ia.start()
    yield f"http://127.0.0.1:{_port(runner)}"
    await runner.cleanup()


@pytest.fixture
async def running_proxy(fake_upstream: str) -> AsyncIterator[RunningProxy]:
    config = Config(as_of=fake_ia.AS_OF, upstream=fake_upstream, port=0)
    manifest = io.StringIO()
    async with aiohttp.ClientSession() as session:
        runner = await start_proxy(config, session, manifest, host="127.0.0.1")
        yield RunningProxy(url=f"http://127.0.0.1:{_port(runner)}", manifest=manifest)
        await runner.cleanup()


@pytest.fixture
async def fetch(running_proxy: RunningProxy):
    async with aiohttp.ClientSession() as client:

        async def fetch_via_proxy(url: str) -> FetchResult:
            async with client.get(url, proxy=running_proxy.url, allow_redirects=False) as response:
                return FetchResult(status=response.status, headers=dict(response.headers), body=await response.read())

        yield fetch_via_proxy


async def test_serves_newest_capture_at_or_before_as_of(fetch) -> None:
    result = await fetch(fake_ia.EXAMPLE_URL)
    assert result.status == 200
    assert result.body == fake_ia.EXAMPLE_BODY
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS
    assert result.headers["Content-Type"] == "text/html"


async def test_manifest_records_served_evidence(fetch, running_proxy: RunningProxy) -> None:
    await fetch(fake_ia.EXAMPLE_URL)
    record = json.loads(running_proxy.manifest.getvalue())
    assert record == {
        "url": fake_ia.EXAMPLE_URL,
        "capture_ts": fake_ia.GOOD_TS,
        "sha256": hashlib.sha256(fake_ia.EXAMPLE_BODY).hexdigest(),
        "size": len(fake_ia.EXAMPLE_BODY),
    }


async def test_no_capture_at_or_before_as_of_is_404(fetch) -> None:
    result = await fetch(fake_ia.FUTURE_ONLY_URL)
    assert result.status == 404
    assert str(fake_ia.AS_OF) in result.body.decode()
    assert "X-Wayback-Timestamp" not in result.headers


async def test_follows_timestamp_canonicalization_redirect(fetch) -> None:
    result = await fetch(fake_ia.CANON_URL)
    assert result.status == 200
    assert result.body == fake_ia.CANON_BODY
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS


async def test_refuses_redirect_drifting_past_as_of(fetch) -> None:
    result = await fetch(fake_ia.DRIFT_URL)
    assert result.status == 403
    assert fake_ia.TOO_NEW_TS in result.body.decode()


async def test_bounces_captured_live_redirect_downgraded_to_http(fetch) -> None:
    result = await fetch(fake_ia.MOVED_URL)
    assert result.status == 302
    # https Location is downgraded so the client's follow-up re-enters the proxy.
    assert result.headers["Location"] == "http://moved.example/new"


async def test_archived_404_is_served_as_of_content(fetch) -> None:
    result = await fetch(fake_ia.GONE_URL)
    assert result.status == 404
    assert result.body == fake_ia.GONE_BODY
    # Distinguishable from the proxy's own "no capture" 404 by the ts header.
    assert result.headers["X-Wayback-Timestamp"] == fake_ia.GOOD_TS


async def test_cdx_failure_maps_to_502(fetch) -> None:
    result = await fetch(fake_ia.CDX_BROKEN_URL)
    assert result.status == 502


async def test_explicit_pinned_capture_within_as_of_served(fetch) -> None:
    result = await fetch(f"http://web.archive.org/web/{fake_ia.GOOD_TS}id_/{fake_ia.EXAMPLE_ORIGINAL}")
    assert result.status == 200
    assert result.body == fake_ia.EXAMPLE_BODY


async def test_explicit_capture_after_as_of_refused(fetch) -> None:
    result = await fetch(f"http://web.archive.org/web/{fake_ia.TOO_NEW_TS}id_/{fake_ia.EXAMPLE_ORIGINAL}")
    assert result.status == 403


async def test_cdx_passthrough_clamps_to_param(fetch) -> None:
    result = await fetch(f"http://web.archive.org{fake_ia.CDX_PATH}?url={fake_ia.EXAMPLE_URL}&output=json")
    assert result.status == 200
    listed_timestamps = {row[1] for row in json.loads(result.body)[1:]}
    assert fake_ia.GOOD_TS in listed_timestamps
    assert fake_ia.TOO_NEW_TS not in listed_timestamps


async def test_other_archive_paths_refused(fetch) -> None:
    result = await fetch("http://web.archive.org/somewhere-else")
    assert result.status == 403


async def test_connect_for_https_rejected(running_proxy: RunningProxy) -> None:
    async with aiohttp.ClientSession() as client:
        with pytest.raises(aiohttp.ClientHttpProxyError) as exc_info:
            await client.get("https://example.com/", proxy=running_proxy.url)
    assert exc_info.value.status == 501


async def test_healthz_and_origin_form(running_proxy: RunningProxy) -> None:
    async with aiohttp.ClientSession() as client:
        async with client.get(f"{running_proxy.url}/healthz") as response:
            assert response.status == 200
        async with client.get(f"{running_proxy.url}/not-a-proxy-request") as response:
            assert response.status == 400


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


def test_config_requires_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYBACK_AS_OF", raising=False)
    with pytest.raises(RuntimeError, match="WAYBACK_AS_OF"):
        Config.from_env()


def test_config_as_of_ts() -> None:
    config = Config(as_of=date(2020, 6, 1), upstream="https://web.archive.org", port=8080)
    assert config.as_of_ts == AS_OF_TS


if __name__ == "__main__":
    pytest_bazel.main()
