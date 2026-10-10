"""Prometheus exporter for an AT&T BGW320 fiber gateway.

The gateway has no SNMP or API, only HTML status pages, and its web server stalls when
polled in a burst. Pages behind the device access code are polled only when the code is set. So a background loop fetches one page at a time with a pause between
them, and `/metrics` serves the last parsed values: a Prometheus scrape never reaches the
gateway. A page whose last fetch or parse failed exports only
`att_gateway_scrape_success{page} 0`, so no stale value outlives the fetch that would have
replaced it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC

import httpx
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from pydantic import SecretStr

from att_gateway.login import log_in
from att_gateway.pages import (
    Broadband,
    Direction,
    Fiber,
    Lan,
    Nat,
    Page,
    Sensor,
    Speed,
    SysInfo,
    parse_broadband,
    parse_fiber,
    parse_lan,
    parse_nat,
    parse_speed,
    parse_sysinfo,
)
from att_gateway.settings import Settings

logger = logging.getLogger(__name__)

type Parsed = SysInfo | Broadband | Fiber | Lan | Nat | Speed

_PARSERS: dict[Page, Callable[[str], Parsed]] = {
    Page.SYSINFO: parse_sysinfo,
    Page.BROADBAND: parse_broadband,
    Page.FIBER: parse_fiber,
    Page.LAN: parse_lan,
    Page.NAT: parse_nat,
    Page.SPEED: parse_speed,
}
# The gateway's clock is on local time in a zone it does not name; its offset from UTC
# is read off the clock rounded to this.
_UTC_OFFSET_STEP_SECONDS = 15 * 60


@dataclass(frozen=True)
class PageResult:
    # Unix time the fetch started.
    fetched_at: float
    duration_seconds: float
    # None when the fetch or the parse failed.
    parsed: Parsed | None


def _gauge(name: str, documentation: str, value: float, labels: dict[str, str] | None = None) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, documentation, labels=list(labels or {}))
    family.add_metric(list((labels or {}).values()), value)
    return family


def _counter(name: str, documentation: str, value: float) -> CounterMetricFamily:
    return CounterMetricFamily(name, documentation, value=value)


def _sysinfo_metrics(sysinfo: SysInfo) -> Iterator[Metric]:
    yield _gauge(
        "att_gateway_info",
        "Gateway model and software.",
        1,
        {"model": sysinfo.model, "firmware": sysinfo.firmware, "hardware": sysinfo.hardware},
    )
    yield _gauge("att_gateway_uptime_seconds", "Time since the gateway last rebooted.", sysinfo.uptime_seconds)


def _broadband_metrics(broadband: Broadband) -> Iterator[Metric]:
    yield _gauge(
        "att_gateway_wan_up", "1 when the gateway reports its broadband connection Up.", broadband.connection_up
    )
    yield _gauge(
        "att_gateway_wan_info",
        "WAN addressing; a changed address shows up as a new series.",
        1,
        {
            "source": broadband.source,
            "network_type": broadband.network_type,
            "ipv4_address": broadband.ipv4_address,
            "ipv4_gateway": broadband.ipv4_gateway,
            "ipv6_address": broadband.ipv6_address,
        },
    )
    yield _gauge("att_gateway_wan_line_up", "1 when the WAN line (ONT side) is Up.", broadband.line_up)
    yield _gauge("att_gateway_wan_line_speed_bps", "WAN line speed.", broadband.line_speed_bps)
    counters = broadband.ipv4
    for direction, packets, octets, drops, errors in (
        ("receive", counters.receive_packets, counters.receive_bytes, counters.receive_drops, counters.receive_errors),
        (
            "transmit",
            counters.transmit_packets,
            counters.transmit_bytes,
            counters.transmit_drops,
            counters.transmit_errors,
        ),
    ):
        yield _counter(f"att_gateway_wan_{direction}_packets", f"WAN IPv4 packets, {direction}, since boot.", packets)
        yield _counter(f"att_gateway_wan_{direction}_bytes", f"WAN IPv4 bytes, {direction}, since boot.", octets)
        yield _counter(f"att_gateway_wan_{direction}_drops", f"WAN IPv4 drops, {direction}, since boot.", drops)
        yield _counter(f"att_gateway_wan_{direction}_errors", f"WAN IPv4 errors, {direction}, since boot.", errors)
    yield _gauge(
        "att_gateway_pon_state",
        "ITU-T G.984/G.9807 ONU state number; 5 (O5, Operation) is the only one carrying traffic.",
        broadband.pon_state,
    )


def _nat_metrics(nat: Nat) -> Iterator[Metric]:
    yield _gauge("att_gateway_nat_sessions_available", "Session table capacity.", nat.sessions_available)
    yield _gauge("att_gateway_nat_sessions_in_use", "Session table entries in use.", nat.sessions_in_use)
    by_source = GaugeMetricFamily(
        "att_gateway_nat_ipv4_sessions", "IPv4 session table entries per source address.", labels=["source"]
    )
    for source, sessions in nat.ipv4_sessions_by_source.items():
        by_source.add_metric([source], sessions)
    yield by_source


def _speed_metrics(speed: Speed, utc_offset_seconds: float | None) -> Iterator[Metric]:
    throughput = GaugeMetricFamily(
        "att_gateway_speedtest_throughput_bps",
        "Throughput of the gateway's latest successful speed test to AT&T, protocol overhead included.",
        labels=["direction"],
    )
    latency = GaugeMetricFamily(
        "att_gateway_speedtest_latency_seconds",
        "Average round-trip latency of the gateway's latest successful speed test.",
        labels=["direction"],
    )
    completed = GaugeMetricFamily(
        "att_gateway_speedtest_completed_timestamp_seconds",
        "When the gateway's latest successful speed test completed.",
        labels=["direction"],
    )
    for direction in Direction:
        tests = [test for test in speed.tests if test.direction == direction and test.status == "Success"]
        if not tests:
            continue
        latest = max(tests, key=lambda test: test.completed)
        throughput.add_metric([direction], latest.throughput_bps)
        latency.add_metric([direction], latest.latency_seconds)
        if utc_offset_seconds is not None:
            completed.add_metric([direction], latest.completed.replace(tzinfo=UTC).timestamp() - utc_offset_seconds)
    yield throughput
    yield latency
    yield completed


def _utc_offset_seconds(results: dict[Page, PageResult]) -> float | None:
    """The gateway clock's offset from UTC, from its sysinfo clock reading against the fetch time."""
    result = results.get(Page.SYSINFO)
    if result is None or not isinstance(result.parsed, SysInfo) or result.parsed.clock is None:
        return None
    skew = result.parsed.clock.replace(tzinfo=UTC).timestamp() - result.fetched_at
    return round(skew / _UTC_OFFSET_STEP_SECONDS) * _UTC_OFFSET_STEP_SECONDS


def _sensor_metrics(name: str, documentation: str, sensor: Sensor) -> Iterator[Metric]:
    yield _gauge(f"att_gateway_optical_{name}", documentation, sensor.value)
    thresholds = GaugeMetricFamily(
        f"att_gateway_optical_{name}_threshold",
        f"The gateway's thresholds for att_gateway_optical_{name}.",
        labels=["level", "bound"],
    )
    exceeded = GaugeMetricFamily(
        f"att_gateway_optical_{name}_threshold_exceeded",
        f"1 when the gateway flags att_gateway_optical_{name} beyond the threshold.",
        labels=["level", "bound"],
    )
    for limit in sensor.limits:
        thresholds.add_metric([limit.level, limit.bound], limit.threshold)
        exceeded.add_metric([limit.level, limit.bound], limit.raised)
    yield thresholds
    yield exceeded


def _fiber_metrics(fiber: Fiber) -> Iterator[Metric]:
    yield _gauge("att_gateway_optical_up", "1 when the optical WAN is operational.", fiber.operational)
    yield _gauge("att_gateway_optical_rx_los", "1 on optical receiver loss of signal.", fiber.rx_los)
    yield _gauge("att_gateway_optical_tx_fault", "1 on optical transmitter fault.", fiber.tx_fault)
    yield _gauge(
        "att_gateway_optical_last_change",
        "Opaque marker that changes whenever the optical link changes state; not a Unix time.",
        fiber.last_change,
    )
    yield from _sensor_metrics("temperature_celsius", "Optical module temperature.", fiber.temperature_celsius)
    yield from _sensor_metrics("tx_power_dbm", "Optical transmit power.", fiber.tx_power_dbm)
    yield from _sensor_metrics("rx_power_dbm", "Optical receive power.", fiber.rx_power_dbm)
    yield from _sensor_metrics("tx_bias", "Optical transmit bias, raw SFF-8472 reading.", fiber.tx_bias)


def _lan_metrics(lan: Lan) -> Iterator[Metric]:
    yield _gauge("att_gateway_dhcp_leases_allocated", "DHCPv4 leases in use.", lan.dhcp_leases_allocated)
    yield _gauge("att_gateway_dhcp_leases_available", "DHCPv4 leases free.", lan.dhcp_leases_available)
    families: dict[str, GaugeMetricFamily | CounterMetricFamily] = {
        "up": GaugeMetricFamily("att_gateway_lan_port_up", "1 when the LAN port has link.", labels=["port"]),
        "speed": GaugeMetricFamily("att_gateway_lan_port_speed_bps", "LAN port link speed.", labels=["port"]),
        "receive_drops": CounterMetricFamily(
            "att_gateway_lan_port_receive_drops", "LAN port receive drops since boot.", labels=["port"]
        ),
        "transmit_drops": CounterMetricFamily(
            "att_gateway_lan_port_transmit_drops", "LAN port transmit drops since boot.", labels=["port"]
        ),
        "receive_errors": CounterMetricFamily(
            "att_gateway_lan_port_receive_errors", "LAN port receive errors since boot.", labels=["port"]
        ),
        "transmit_errors": CounterMetricFamily(
            "att_gateway_lan_port_transmit_errors", "LAN port transmit errors since boot.", labels=["port"]
        ),
    }
    for port in lan.ports:
        label = [str(port.port)]
        families["up"].add_metric(label, port.up)
        families["speed"].add_metric(label, port.speed_bps)
        families["receive_drops"].add_metric(label, port.receive_drops)
        families["transmit_drops"].add_metric(label, port.transmit_drops)
        families["receive_errors"].add_metric(label, port.receive_errors)
        families["transmit_errors"].add_metric(label, port.transmit_errors)
    yield from families.values()


class GatewayCollector(Collector):
    def __init__(self) -> None:
        self.results: dict[Page, PageResult] = {}

    def collect(self) -> Iterator[Metric]:
        success = GaugeMetricFamily(
            "att_gateway_scrape_success", "1 when the page's last fetch and parse succeeded.", labels=["page"]
        )
        duration = GaugeMetricFamily(
            "att_gateway_scrape_duration_seconds", "How long the page's last fetch took.", labels=["page"]
        )
        # A snapshot: the poll loop replaces entries while a scrape iterates.
        results = dict(self.results)
        for page, result in results.items():
            success.add_metric([page], result.parsed is not None)
            duration.add_metric([page], result.duration_seconds)
        yield success
        yield duration
        for result in results.values():
            match result.parsed:
                case SysInfo() as sysinfo:
                    yield from _sysinfo_metrics(sysinfo)
                case Broadband() as broadband:
                    yield from _broadband_metrics(broadband)
                case Fiber() as fiber:
                    yield from _fiber_metrics(fiber)
                case Lan() as lan:
                    yield from _lan_metrics(lan)
                case Nat() as nat:
                    yield from _nat_metrics(nat)
                case Speed() as speed:
                    yield from _speed_metrics(speed, _utc_offset_seconds(results))
                case None:
                    pass


async def _fetch_parsed(client: httpx.AsyncClient, page: Page) -> Parsed:
    response = await client.get(f"/cgi-bin/{page}.ha")
    response.raise_for_status()
    return _PARSERS[page](response.text)


async def fetch_page(
    client: httpx.AsyncClient, page: Page, access_code: SecretStr | None, page_gap_seconds: float
) -> PageResult:
    """`access_code` is for a page behind it; such a page answers 200 with the login form
    until the session logs in, so a parse failure there logs in and fetches it again."""
    fetched_at = time.time()
    started = time.monotonic()
    parsed: Parsed | None
    try:
        try:
            parsed = await _fetch_parsed(client, page)
        except ValueError:
            if access_code is None:
                raise
            await log_in(client, access_code, page_gap_seconds)
            parsed = await _fetch_parsed(client, page)
    except httpx.HTTPError, ValueError:
        logger.warning("fetching %s failed", page, exc_info=True)
        parsed = None
    return PageResult(fetched_at=fetched_at, duration_seconds=time.monotonic() - started, parsed=parsed)


async def poll_once(
    client: httpx.AsyncClient, collector: GatewayCollector, access_code: SecretStr | None, page_gap_seconds: float
) -> None:
    """Fetch every page in turn, never two at once; those behind the access code only when it is set."""
    pages = [page for page in Page if access_code is not None or not page.needs_login]
    for i, page in enumerate(pages):
        if i:
            await asyncio.sleep(page_gap_seconds)
        collector.results[page] = await fetch_page(
            client, page, access_code if page.needs_login else None, page_gap_seconds
        )


async def poll_forever(settings: Settings, collector: GatewayCollector) -> None:
    async with httpx.AsyncClient(base_url=str(settings.url), timeout=settings.request_timeout_seconds) as client:
        while True:
            started = time.monotonic()
            await poll_once(client, collector, settings.access_code, settings.page_gap_seconds)
            await asyncio.sleep(max(0, settings.poll_interval_seconds - (time.monotonic() - started)))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    collector = GatewayCollector()
    REGISTRY.register(collector)
    start_http_server(settings.listen_port)
    asyncio.run(poll_forever(settings, collector))


if __name__ == "__main__":
    main()
