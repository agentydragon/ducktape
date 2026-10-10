"""Parses pages saved from a live BGW320-500 on firmware 6.35.8 on 2026-10-09
(`testdata/`), with the serial numbers, MAC addresses, public addresses and SSID replaced
by documentation values. The expected values are read off the rendered pages."""

from pathlib import Path

import pytest
import pytest_bazel

from cluster.exporters.att_gateway.pages import (
    Bound,
    Level,
    Limit,
    parse_broadband,
    parse_fiber,
    parse_lan,
    parse_sysinfo,
)

_TESTDATA = Path(__file__).parent / "testdata"


def _page(name: str) -> str:
    return (_TESTDATA / f"{name}.html").read_text()


def test_sysinfo() -> None:
    sysinfo = parse_sysinfo(_page("sysinfo"))
    assert (sysinfo.model, sysinfo.firmware, sysinfo.uptime_seconds) == ("BGW320-500", "6.35.8", 632802)


def test_broadband() -> None:
    broadband = parse_broadband(_page("broadbandstatistics"))
    assert broadband.connection_up
    assert broadband.line_speed_bps == 10_000_000_000
    # 64-bit: well past 2**32.
    assert broadband.ipv4.receive_bytes == 2875130071354
    assert (broadband.pon_link_status, broadband.pon_state) == ("OPERATION (O5)", 5)


def test_fiber_converts_tenths_of_dbm() -> None:
    fiber = parse_fiber(_page("fiberstat"))
    assert (fiber.operational, fiber.rx_los, fiber.tx_fault) == (True, False, False)
    assert fiber.rx_power_dbm.value == -14.8
    assert fiber.tx_power_dbm.value == 6.1
    assert fiber.temperature_celsius.value == 41
    assert Limit(level=Level.WARNING, bound=Bound.LOW, threshold=-28.5, raised=False) in fiber.rx_power_dbm.limits
    assert len(fiber.rx_power_dbm.limits) == 4


def test_lan_ports() -> None:
    lan = parse_lan(_page("lanstatistics"))
    port1, *others = lan.ports
    assert (port1.port, port1.up, port1.speed_bps, port1.receive_errors) == (1, True, 2_500_000_000, 18474)
    assert [port.port for port in others] == [2, 3, 4]
    assert not any(port.up for port in others)
    assert (lan.dhcp_leases_allocated, lan.dhcp_leases_available) == (8, 182)


def test_login_page_is_rejected() -> None:
    """Pages behind the access code answer HTTP 200 with the login form."""
    login = "<html><head><title>Login</title></head><body><form><input name='nonce'></form></body></html>"
    with pytest.raises(ValueError, match="table not found"):
        parse_fiber(login)


if __name__ == "__main__":
    pytest_bazel.main()
