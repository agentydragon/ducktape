import asyncio
from pathlib import Path

import httpx
import pytest_bazel
from prometheus_client import CollectorRegistry

from cluster.exporters.att_gateway.main import GatewayCollector, poll_once
from cluster.exporters.att_gateway.pages import Page

_TESTDATA = Path(__file__).parent / "testdata"
_BASE_URL = "http://gateway.test"


async def _poll(handler: httpx.MockTransport) -> CollectorRegistry:
    collector = GatewayCollector()
    registry = CollectorRegistry()
    registry.register(collector)
    async with httpx.AsyncClient(transport=handler, base_url=_BASE_URL) as client:
        await poll_once(client, collector, page_gap_seconds=0)
    return registry


def _serve_saved_pages(request: httpx.Request) -> httpx.Response:
    page = request.url.path.removeprefix("/cgi-bin/").removesuffix(".ha")
    return httpx.Response(200, text=(_TESTDATA / f"{page}.html").read_text())


async def test_exports_every_page() -> None:
    registry = await _poll(httpx.MockTransport(_serve_saved_pages))
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


async def test_failed_page_drops_its_metrics_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/cgi-bin/{Page.FIBER}.ha":
            return httpx.Response(500)
        return _serve_saved_pages(request)

    registry = await _poll(httpx.MockTransport(handler))
    assert registry.get_sample_value("att_gateway_scrape_success", {"page": Page.FIBER}) == 0
    assert not any(metric.name.startswith("att_gateway_optical_") for metric in registry.collect())
    assert registry.get_sample_value("att_gateway_uptime_seconds") == 632802


async def test_pages_are_fetched_one_at_a_time() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield, so a concurrent fetch would overlap this one.
        await asyncio.sleep(0)
        response = _serve_saved_pages(request)
        in_flight -= 1
        return response

    await _poll(httpx.MockTransport(handler))
    assert peak == 1


if __name__ == "__main__":
    pytest_bazel.main()
