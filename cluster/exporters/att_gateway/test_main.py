import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest_bazel
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from cluster.exporters.att_gateway.main import GatewayCollector, PageResult, poll_once
from cluster.exporters.att_gateway.pages import Page, parse_speed, parse_sysinfo

_TESTDATA = Path(__file__).parent / "testdata"
_BASE_URL = "http://gateway.test"
_ACCESS_CODE = "test-code-1"
_NONCE = "0123abcd"
_LOGIN_FORM = f'<html><body><form><input type="hidden" name="nonce" value="{_NONCE}" /></form></body></html>'


class FakeGateway:
    """Serves the saved pages; those behind the access code only to a session that posted the
    login form with `_ACCESS_CODE` hashed with the nonce, as the real one checks it."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        page = request.url.path.removeprefix("/cgi-bin/").removesuffix(".ha")
        self.requested.append(page)
        if page == "login":
            if request.method == "GET":
                return httpx.Response(200, text=_LOGIN_FORM, headers={"Set-Cookie": "SessionID=anonymous"})
            form = dict(httpx.QueryParams(request.content.decode()))
            if form["hashpassword"] == hashlib.md5(f"{_ACCESS_CODE}{_NONCE}".encode()).hexdigest():
                return httpx.Response(200, text="<html>Status</html>", headers={"Set-Cookie": "SessionID=granted"})
            return httpx.Response(200, text=_LOGIN_FORM)
        if Page(page).needs_login and request.headers.get("Cookie") != "SessionID=granted":
            return httpx.Response(200, text=_LOGIN_FORM)
        return httpx.Response(200, text=(_TESTDATA / f"{page}.html").read_text())


async def _poll(handler: httpx.MockTransport, access_code: str | None = _ACCESS_CODE) -> CollectorRegistry:
    collector = GatewayCollector()
    registry = CollectorRegistry()
    registry.register(collector)
    async with httpx.AsyncClient(transport=handler, base_url=_BASE_URL) as client:
        await poll_once(
            client, collector, SecretStr(access_code) if access_code is not None else None, page_gap_seconds=0
        )
    return registry


async def test_exports_every_page() -> None:
    registry = await _poll(httpx.MockTransport(FakeGateway().handle))
    for page in Page:
        assert registry.get_sample_value("att_gateway_scrape_success", {"page": page}) == 1
    assert registry.get_sample_value("att_gateway_uptime_seconds") == 632802
    assert registry.get_sample_value("att_gateway_wan_receive_bytes_total") == 2875130071354
    assert registry.get_sample_value("att_gateway_optical_rx_power_dbm") == -14.8
    assert (
        registry.get_sample_value("att_gateway_optical_rx_power_dbm_threshold", {"level": "warning", "bound": "low"})
        == -28.5
    )
    assert registry.get_sample_value("att_gateway_lan_port_receive_errors_total", {"port": "1"}) == 18474
    assert registry.get_sample_value("att_gateway_nat_sessions_in_use") == 116
    assert registry.get_sample_value("att_gateway_nat_ipv4_sessions", {"source": "192.168.1.72"}) == 44
    assert registry.get_sample_value("att_gateway_speedtest_throughput_bps", {"direction": "downstream"}) == 1184.49e6


async def test_without_access_code_skips_pages_behind_it() -> None:
    gateway = FakeGateway()
    registry = await _poll(httpx.MockTransport(gateway.handle), access_code=None)
    assert set(gateway.requested) == {page.value for page in Page if not page.needs_login}
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.NAT}) is None
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.SYSINFO}) == 1


async def test_wrong_access_code_fails_only_pages_behind_it() -> None:
    registry = await _poll(httpx.MockTransport(FakeGateway().handle), access_code="wrong-code")
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.NAT}) == 0
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.SPEED}) == 0
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.LAN}) == 1


def test_speed_test_time_uses_gateway_clock_offset() -> None:
    sysinfo = parse_sysinfo((_TESTDATA / "sysinfo.html").read_text())
    assert sysinfo.clock == datetime(2026, 10, 9, 17, 28, 29)
    collector = GatewayCollector()
    # The gateway's clock runs 7 hours behind UTC (Pacific daylight time); fetched a few seconds after it rendered.
    collector.results[Page.SYSINFO] = PageResult(
        fetched_at=datetime(2026, 10, 10, 0, 28, 33, tzinfo=UTC).timestamp(), duration_seconds=1, parsed=sysinfo
    )
    collector.results[Page.SPEED] = PageResult(
        fetched_at=0, duration_seconds=1, parsed=parse_speed((_TESTDATA / "speed.html").read_text())
    )
    registry = CollectorRegistry()
    registry.register(collector)
    # The latest downstream test completed at 06:12:25 on the gateway's clock.
    assert (
        registry.get_sample_value("att_gateway_speedtest_completed_timestamp_seconds", {"direction": "downstream"})
        == datetime(2026, 10, 9, 13, 12, 25, tzinfo=UTC).timestamp()
    )


async def test_failed_page_drops_its_metrics_only() -> None:
    gateway = FakeGateway()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/cgi-bin/{Page.FIBER}.ha":
            return httpx.Response(500)
        return gateway.handle(request)

    registry = await _poll(httpx.MockTransport(handler))
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.FIBER}) == 0
    assert not any(metric.name.startswith("att_gateway_optical_") for metric in registry.collect())
    assert registry.get_sample_value("att_gateway_uptime_seconds") == 632802


async def test_pages_are_fetched_one_at_a_time() -> None:
    gateway = FakeGateway()
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield, so a concurrent fetch would overlap this one.
        await asyncio.sleep(0)
        response = gateway.handle(request)
        in_flight -= 1
        return response

    await _poll(httpx.MockTransport(handler))
    assert peak == 1


if __name__ == "__main__":
    pytest_bazel.main()
