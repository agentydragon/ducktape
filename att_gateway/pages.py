"""Parsers for the BGW320's status pages (`/cgi-bin/<page>.ha`), written against firmware
6.35.8. Every page is a set of `<table summary="...">` blocks of label/value rows;
the parsers look tables up by that summary and rows by their label, and raise on anything
missing, so a firmware change that moves a field fails loudly instead of exporting zeros.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from bs4 import BeautifulSoup, Tag

from att_gateway.settings import Syslog, SyslogLevel


class Page(StrEnum):
    SYSINFO = "sysinfo"
    BROADBAND = "broadbandstatistics"
    FIBER = "fiberstat"
    LAN = "lanstatistics"
    # Behind the device access code.
    NAT = "nattable"
    SPEED = "speed"

    @property
    def needs_login(self) -> bool:
        return self in {Page.NAT, Page.SPEED}


@dataclass(frozen=True)
class SysInfo:
    model: str
    firmware: str
    hardware: str
    uptime_seconds: int
    # The gateway's wall clock in its own time zone, which the page does not name; None
    # while the WAN is down and the gateway has no time reference.
    clock: datetime | None


@dataclass(frozen=True)
class InterfaceCounters:
    receive_packets: int
    transmit_packets: int
    receive_bytes: int
    transmit_bytes: int
    receive_drops: int
    transmit_drops: int
    receive_errors: int
    transmit_errors: int


@dataclass(frozen=True)
class Broadband:
    source: str
    network_type: str
    connection_up: bool
    ipv4_address: str
    ipv4_gateway: str
    ipv6_address: str
    line_up: bool
    line_speed_bps: int
    ipv4: InterfaceCounters
    pon_link_status: str
    # The ITU-T G.984/G.9807 ONU state number: 5 (O5, "Operation") is the only state
    # that carries traffic; 1-4 are the ranging steps after a loss of signal.
    pon_state: int


class Bound(StrEnum):
    LOW = "low"
    HIGH = "high"


class Level(StrEnum):
    ALARM = "alarm"
    WARNING = "warning"


@dataclass(frozen=True)
class Limit:
    level: Level
    bound: Bound
    threshold: float
    # The gateway's own verdict on the current value against `threshold`.
    raised: bool


@dataclass(frozen=True)
class Sensor:
    value: float
    limits: tuple[Limit, ...]


@dataclass(frozen=True)
class Fiber:
    operational: bool
    rx_los: bool
    tx_fault: bool
    # Changes whenever the optical link changes state. The value is not a Unix time the
    # rest of the page agrees with, so it is only good for detecting that a change happened.
    last_change: int
    temperature_celsius: Sensor
    tx_power_dbm: Sensor
    rx_power_dbm: Sensor
    # Raw SFF-8472 bias reading; the page's own unit note does not match plausible values.
    tx_bias: Sensor


@dataclass(frozen=True)
class LanPort:
    port: int
    up: bool
    speed_bps: int
    receive_drops: int
    transmit_drops: int
    receive_errors: int
    transmit_errors: int


@dataclass(frozen=True)
class Lan:
    dhcp_leases_allocated: int
    dhcp_leases_available: int
    ports: tuple[LanPort, ...]


@dataclass(frozen=True)
class Nat:
    sessions_available: int
    sessions_in_use: int
    # Rows of the session table per IPv4 source address. LAN hosts' outbound sessions are the
    # ones the gateway translates; the gateway's own WAN address shows up for its own.
    ipv4_sessions_by_source: dict[str, int]


class Direction(StrEnum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


@dataclass(frozen=True)
class SpeedTest:
    # In the gateway's time zone, like `SysInfo.clock`.
    completed: datetime
    direction: Direction
    throughput_bps: float
    latency_seconds: float
    # "Success" on a completed test; throughput and latency mean nothing otherwise.
    status: str


@dataclass(frozen=True)
class Speed:
    """The gateway's own speed tests against AT&T's server, newest first."""

    tests: tuple[SpeedTest, ...]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _table(soup: BeautifulSoup, summary: str) -> Tag:
    table = soup.find("table", attrs={"summary": summary})
    if not isinstance(table, Tag):
        raise ValueError(f"table not found: {summary=}")
    return table


def _cells(row: Tag) -> list[str]:
    return [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]


def _fields(table: Tag) -> dict[str, str]:
    """Label → value for the table's two-cell rows."""
    return {cells[0]: cells[1] for row in table.find_all("tr") if len(cells := _cells(row)) == 2}


def _field[T](fields: dict[str, T], label: str) -> T:
    if label not in fields:
        raise ValueError(f"field not found: {label=}")
    return fields[label]


def _up(value: str) -> bool:
    match value.lower():
        case "up":
            return True
        case "down":
            return False
    raise ValueError(f"unexpected link state: {value=}")


def _flag(value: str) -> bool:
    match value:
        case "0":
            return False
        case "1":
            return True
    raise ValueError(f"unexpected flag: {value=}")


def parse_sysinfo(html: str) -> SysInfo:
    fields = _fields(_table(_soup(html), "This table includes system information about the device and its software"))
    return SysInfo(
        model=_field(fields, "Model Number"),
        firmware=_field(fields, "Software Version"),
        hardware=_field(fields, "Hardware Version"),
        uptime_seconds=int(_field(fields, "Time Since Last Reboot")),
        # A trailing Z means the gateway has no time zone set and runs on UTC, as do its logs.
        clock=datetime.fromisoformat(clock.removesuffix("Z"))
        if (clock := _field(fields, "Current Date/Time"))
        else None,
    )


_PON_STATE = re.compile(r"\(O(\d)\)")


def parse_broadband(html: str) -> Broadband:
    soup = _soup(html)
    summary = _fields(_table(soup, "Summary of the most important WAN information"))
    line = _fields(_table(soup, "Ethernet Statistics Table"))
    ipv6 = _fields(_table(soup, "IPv6 Table"))
    counters = _fields(_table(soup, "Ethernet IPv4 Statistics Table"))
    pon_link_status = _field(_fields(_table(soup, "GPON Status Table")), "PON Link Status")
    pon_state = _PON_STATE.search(pon_link_status)
    if pon_state is None:
        raise ValueError(f"no ONU state in {pon_link_status=}")
    return Broadband(
        source=_field(summary, "Broadband Connection Source"),
        network_type=_field(summary, "Broadband Network Type"),
        connection_up=_up(_field(summary, "Broadband Connection")),
        ipv4_address=_field(summary, "Broadband IPv4 Address"),
        ipv4_gateway=_field(summary, "Gateway IPv4 Address"),
        ipv6_address=_field(ipv6, "Global Unicast IPv6 Address"),
        line_up=_up(_field(line, "Line State")),
        line_speed_bps=int(_field(line, "Current Speed (Mbps)")) * 1_000_000,
        ipv4=InterfaceCounters(
            receive_packets=int(_field(counters, "Receive Packets")),
            transmit_packets=int(_field(counters, "Transmit Packets")),
            receive_bytes=int(_field(counters, "Receive Bytes")),
            transmit_bytes=int(_field(counters, "Transmit Bytes")),
            receive_drops=int(_field(counters, "Receive Drops")),
            transmit_drops=int(_field(counters, "Transmit Drops")),
            receive_errors=int(_field(counters, "Receive Errors")),
            transmit_errors=int(_field(counters, "Transmit Errors")),
        ),
        pon_link_status=pon_link_status,
        pon_state=int(pon_state.group(1)),
    )


_CURRENTLY = re.compile(r"Currently\s+(-?\d+)")
_LIMIT = re.compile(r"^([01])\s+\(Threshold\s+(-?\d+)\)$")


def _sensor(soup: BeautifulSoup, summary: str, divisor: int) -> Sensor:
    """A DDM sensor section: an `<h1>Name Currently N</h1>` heading over its threshold table,
    whose cells read `<flag> (Threshold <n>)`. The page's integers divided by `divisor` are
    the sensor's unit."""
    table = _table(soup, summary)
    heading = table.find_previous("h1")
    current = _CURRENTLY.search(heading.get_text(" ", strip=True)) if heading else None
    if current is None:
        raise ValueError(f"no current value above {summary=}")
    limits: list[Limit] = []
    for row in table.find_all("tr"):
        level, *cells = _cells(row)
        if not level:
            continue
        for bound, cell in zip((Bound.LOW, Bound.HIGH), cells, strict=True):
            parsed = _LIMIT.match(cell)
            if parsed is None:
                raise ValueError(f"unexpected limit cell: {summary=} {cell=}")
            limits.append(
                Limit(
                    level=Level(level.lower()),
                    bound=bound,
                    threshold=int(parsed.group(2)) / divisor,
                    raised=_flag(parsed.group(1)),
                )
            )
    return Sensor(value=int(current.group(1)) / divisor, limits=tuple(limits))


def parse_fiber(html: str) -> Fiber:
    soup = _soup(html)
    fields = _fields(_table(soup, "Table of Fiber stats"))
    return Fiber(
        operational=_up(_field(fields, "Optical WAN Operational Status")),
        rx_los=_flag(_field(fields, "Rx LOS State")),
        tx_fault=_flag(_field(fields, "Tx Fault State")),
        last_change=int(_field(fields, "Last Change")),
        temperature_celsius=_sensor(soup, "Temperature table", 1),
        # The page documents both powers as tenths of a dBm.
        tx_power_dbm=_sensor(soup, "Tx Power table", 10),
        rx_power_dbm=_sensor(soup, "Rx Power table", 10),
        tx_bias=_sensor(soup, "Tx Bias table", 1),
    )


def parse_lan(html: str) -> Lan:
    soup = _soup(html)
    status = _fields(_table(soup, "This table displays the critical LAN status of the device."))
    rows = {
        cells[0]: cells[1:]
        for row in _table(soup, "LAN Ethernet Statistics Table").find_all("tr")
        if (cells := _cells(row))
    }
    ports = _field(rows, "")
    # Per-port byte (and likely packet) counters are 32-bit and wrap within seconds at
    # multi-gigabit rates, so only the low-rate drop and error counters are exported.
    return Lan(
        dhcp_leases_allocated=int(_field(status, "DHCP Leases Allocated")),
        dhcp_leases_available=int(_field(status, "DHCP Leases Available")),
        ports=tuple(
            LanPort(
                port=int(name.removeprefix("Port ")),
                up=_up(_field(rows, "State")[i]),
                speed_bps=int(_field(rows, "Transmit Speed")[i]),
                receive_drops=int(_field(rows, "Receive Dropped")[i]),
                transmit_drops=int(_field(rows, "Transmit Dropped")[i]),
                receive_errors=int(_field(rows, "Receive Errors")[i]),
                transmit_errors=int(_field(rows, "Transmit Errors")[i]),
            )
            for i, name in enumerate(ports)
        ),
    )


def _rows(table: Tag) -> list[list[str]]:
    """The table's data rows: those with `<td>` cells, skipping header rows."""
    return [_cells(row) for row in table.find_all("tr") if row.find("td")]


def parse_nat(html: str) -> Nat:
    soup = _soup(html)
    summary = _fields(_table(soup, "This table displays a summary of session information."))
    sessions = _table(soup, "Summary of nattable connections")
    header = [cell.get_text(" ", strip=True) for cell in sessions.find_all("th")]
    family, source = header.index("IP Family"), header.index("Source Address")
    return Nat(
        sessions_available=int(_field(summary, "Total sessions available")),
        sessions_in_use=int(_field(summary, "Total sessions in use")),
        ipv4_sessions_by_source=dict(Counter(row[source] for row in _rows(sessions) if row[family] == "ipv4")),
    )


def parse_speed(html: str) -> Speed:
    return Speed(
        tests=tuple(
            SpeedTest(
                completed=datetime.strptime(completed, "%m/%d/%Y %H:%M:%S"),
                direction=Direction(direction),
                throughput_bps=float(throughput_mbps) * 1_000_000,
                latency_seconds=float(latency_ms) / 1000,
                status=status,
            )
            for completed, direction, throughput_mbps, _overhead, latency_ms, status in _rows(
                _table(_soup(html), "Table of Speed Test Result History")
            )
        )
    )


@dataclass(frozen=True)
class SyslogPage:
    syslog: Syslog
    # Binds a post of the form to this session.
    nonce: str


def _selected(form: Tag, name: str) -> str:
    select = form.find("select", attrs={"name": name})
    option = select.find("option", selected=True) if isinstance(select, Tag) else None
    if not isinstance(option, Tag):
        raise ValueError(f"no selected option: {name=}")
    return str(option["value"])


def _input(form: Tag, name: str) -> str:
    field = form.find("input", attrs={"name": name})
    if not isinstance(field, Tag):
        raise ValueError(f"input not found: {name=}")
    return str(field.get("value", ""))


def parse_syslog(html: str) -> SyslogPage:
    """`syslog.ha`, behind the access code. While syslog is off the page still shows the
    other fields' values, disabled."""
    form = _soup(html).find("form", attrs={"action": "/cgi-bin/syslog.ha"})
    if not isinstance(form, Tag):
        raise ValueError("syslog form not found")
    enabled = _selected(form, "syslog")
    if enabled not in {"on", "off"}:
        raise ValueError(f"unexpected syslog state: {enabled=}")
    return SyslogPage(
        syslog=Syslog(
            enabled=enabled == "on",
            server=_input(form, "location"),
            port=int(_input(form, "port")),
            level=SyslogLevel(_selected(form, "level")),
        ),
        nonce=_input(form, "nonce"),
    )


def syslog_form(syslog: Syslog, nonce: str) -> dict[str, str]:
    """The `syslog.ha` post that saves `syslog`."""
    return {
        "nonce": nonce,
        "syslog": "on" if syslog.enabled else "off",
        "location": syslog.server,
        "port": str(syslog.port),
        "level": syslog.level,
        "Save": "Save",
    }
