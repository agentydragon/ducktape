"""Parses pages saved from a live BGW320-500 on firmware 6.35.8 on 2026-10-09
(`testdata/`; `nattable`, `speed` and `syslog` on 2026-10-10, logged in), with the serial
numbers, MAC addresses, public and global addresses, SSID and form nonce replaced by
documentation values. The expected values are read off the rendered pages."""

from datetime import datetime
from pathlib import Path

import pytest
import pytest_bazel

from att_gateway.pages import (
    Bound,
    Direction,
    Level,
    Limit,
    parse_broadband,
    parse_fiber,
    parse_lan,
    parse_nat,
    parse_speed,
    parse_sysinfo,
    parse_syslog,
    syslog_form,
)
from att_gateway.settings import Syslog, SyslogLevel

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


def test_nat_counts_ipv4_sessions_per_source() -> None:
    nat = parse_nat(_page("nattable"))
    assert (nat.sessions_available, nat.sessions_in_use) == (32767, 116)
    # IPv6 rows are left out; the gateway's own WAN address counts like a LAN host.
    assert nat.ipv4_sessions_by_source == {
        "192.168.1.66": 3,
        "192.168.1.71": 7,
        "192.168.1.72": 44,
        "192.168.1.90": 25,
        "192.168.1.97": 1,
        "203.0.113.35": 5,
    }


def test_speed_history() -> None:
    newest, *older = parse_speed(_page("speed")).tests
    assert newest.completed == datetime(2026, 10, 9, 6, 12, 33)
    assert (newest.direction, newest.throughput_bps, newest.latency_seconds, newest.status) == (
        Direction.UPSTREAM,
        1275.96e6,
        0.013,
        "Success",
    )
    assert len(older) == 7


def test_login_page_is_rejected() -> None:
    """Pages behind the access code answer HTTP 200 with the login form."""
    login = "<html><head><title>Login</title></head><body><form><input name='nonce'></form></body></html>"
    with pytest.raises(ValueError, match="table not found"):
        parse_fiber(login)


def test_syslog() -> None:
    # Saved with syslog off, as the gateway ships.
    page = parse_syslog(_page("syslog"))
    assert page.syslog == Syslog(enabled=False, server="", port=514, level=SyslogLevel.ERROR)
    assert page.nonce == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert syslog_form(Syslog(enabled=True, server="192.0.2.1", port=1514, level=SyslogLevel.NOTICE), "ab12") == {
        "nonce": "ab12",
        "syslog": "on",
        "location": "192.0.2.1",
        "port": "1514",
        "level": "Notice",
        "Save": "Save",
    }


def test_syslog_rejects_login_page() -> None:
    with pytest.raises(ValueError, match="syslog form not found"):
        parse_syslog('<html><body><form><input type="hidden" name="nonce" value="ab12" /></form></body></html>')


if __name__ == "__main__":
    pytest_bazel.main()
