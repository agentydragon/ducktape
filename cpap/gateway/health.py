"""Self-healing check for the CPAP gateway VM's USB WiFi adapter.

Runs once per invocation (a systemd timer fires it every minute): observe the adapter,
classify the gateway, and act. Transitions and recovery steps are logged to stderr, which
journald forwards to the serial console and so to Loki (`container="guest-console-log"`).
"""

import argparse
import logging
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")
# The first escalation waits this many failed checks, giving NetworkManager time after
# boot or after the previous reset; later escalations follow at the same interval.
ESCALATION_INTERVAL = 5


class State(StrEnum):
    CONNECTED = "connected"
    # The card's AP is in the scan but not associated: retry the NetworkManager profile.
    CARD_VISIBLE = "card-visible"
    # The radio sees other networks but not the card. Expected while the CPAP travels.
    CARD_AWAY = "card-away"
    # No WiFi device, or a scan that sees no network at all: the adapter is wedged.
    NO_WIFI_DEVICE = "no-wifi-device"
    RADIO_DEAF = "radio-deaf"


WEDGED = {State.NO_WIFI_DEVICE, State.RADIO_DEAF}


class HealthState(BaseModel):
    """Persisted across invocations in a tmpfs, so a reboot starts from scratch."""

    state: State | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class Observation:
    wifi_iface: str | None
    has_card_route: bool
    # Scan results; empty for a deaf radio. Not taken when there is no interface or the
    # card route is already up.
    ssids: frozenset[str]


@dataclass(frozen=True)
class StartProxy:
    pass


@dataclass(frozen=True)
class StopProxy:
    pass


@dataclass(frozen=True)
class ConnectCard:
    pass


@dataclass(frozen=True)
class ReloadDriver:
    pass


@dataclass(frozen=True)
class ReauthorizeAdapter:
    pass


Action = StartProxy | StopProxy | ConnectCard | ReloadDriver | ReauthorizeAdapter


def classify(observation: Observation, card_ssid: str) -> State:
    if observation.wifi_iface is None:
        return State.NO_WIFI_DEVICE
    if observation.has_card_route:
        return State.CONNECTED
    if card_ssid in observation.ssids:
        return State.CARD_VISIBLE
    if observation.ssids:
        return State.CARD_AWAY
    return State.RADIO_DEAF


def plan(state: State, previous: HealthState) -> tuple[HealthState, list[Action]]:
    if state == State.CONNECTED:
        return HealthState(state=state), [StartProxy()]
    if state == State.CARD_VISIBLE:
        return HealthState(state=state), [StopProxy(), ConnectCard()]
    if state == State.CARD_AWAY:
        return HealthState(state=state), [StopProxy()]
    failures = previous.consecutive_failures + 1
    actions: list[Action] = [StopProxy()]
    if failures % ESCALATION_INTERVAL == 0:
        # Alternate: a driver reload first, a USB re-authorization next time round.
        actions.append(ReloadDriver() if failures % (2 * ESCALATION_INTERVAL) else ReauthorizeAdapter())
    return HealthState(state=state, consecutive_failures=failures), actions


def parse_nmcli_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line; nmcli escapes `:` and `\\` inside fields."""
    fields, current, chars = [], [], iter(line)
    for char in chars:
        if char == "\\":
            current.append(next(chars, ""))
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def find_adapters(sysfs_usb_devices: Path, vendor_id: str, product_id: str) -> list[Path]:
    def read(device: Path, attribute: str) -> str | None:
        path = device / attribute
        return path.read_text().strip() if path.exists() else None

    return sorted(
        device
        for device in sysfs_usb_devices.iterdir()
        if read(device, "idVendor") == vendor_id and read(device, "idProduct") == product_id
    )


@dataclass(frozen=True)
class Gateway:
    card_ssid: str
    card_subnet: str
    connection: str
    proxy_unit: str
    driver: str
    vendor_id: str
    product_id: str
    sysfs_usb_devices: Path

    def observe(self) -> Observation:
        devices = _run("nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status").splitlines()
        wifi_iface = next((f[0] for f in map(parse_nmcli_terse, devices) if f[1:] == ["wifi"]), None)
        has_card_route = bool(_run("ip", "-4", "route", "show", self.card_subnet).strip())
        if wifi_iface is None or has_card_route:
            return Observation(wifi_iface=wifi_iface, has_card_route=has_card_route, ssids=frozenset())
        scan = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", wifi_iface, "--rescan", "yes"],
            capture_output=True,
            text=True,
            check=False,
        )
        if scan.returncode:
            # A wedged radio fails its scan; that is the evidence for radio-deaf.
            logger.warning("WiFi scan on %s failed: %s", wifi_iface, scan.stderr.strip())
        ssids = frozenset(ssid for line in scan.stdout.splitlines() if (ssid := parse_nmcli_terse(line)[0]))
        return Observation(wifi_iface=wifi_iface, has_card_route=False, ssids=ssids)

    def apply(self, action: Action, state: HealthState) -> None:
        match action:
            case StartProxy():
                _run("systemctl", "start", "--no-block", self.proxy_unit)
            case StopProxy():
                _run("systemctl", "stop", self.proxy_unit)
            case ConnectCard():
                # Associating fails while the card is out of range or still booting; the
                # next check retries.
                result = subprocess.run(
                    ["nmcli", "connection", "up", self.connection], capture_output=True, text=True, check=False
                )
                if result.returncode:
                    logger.warning("nmcli connection up %s failed: %s", self.connection, result.stderr.strip())
            case ReloadDriver():
                self._log_adapters()
                logger.info("%s for %d checks; reloading %s", state.state, state.consecutive_failures, self.driver)
                # Removal fails when the module is already gone; loading it is what matters.
                removal = subprocess.run(["modprobe", "-r", self.driver], capture_output=True, text=True, check=False)
                if removal.returncode:
                    logger.warning("modprobe -r %s failed: %s", self.driver, removal.stderr.strip())
                _run("modprobe", self.driver)
            case ReauthorizeAdapter():
                adapters = self._log_adapters()
                logger.info(
                    "%s for %d checks; re-authorizing %d USB adapter(s)",
                    state.state,
                    state.consecutive_failures,
                    len(adapters),
                )
                for adapter in adapters:
                    # Deauthorizing unbinds the drivers before the write returns, so the
                    # re-authorization that follows re-enumerates the device from scratch.
                    (adapter / "authorized").write_text("0")
                    (adapter / "authorized").write_text("1")

    def _log_adapters(self) -> list[Path]:
        adapters = find_adapters(self.sysfs_usb_devices, self.vendor_id, self.product_id)
        if not adapters:
            logger.warning("no USB device %s:%s present", self.vendor_id, self.product_id)
        for adapter in adapters:
            logger.info("adapter present at %s (authorized=%s)", adapter, (adapter / "authorized").read_text().strip())
        return adapters


def _run(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def check(gateway: Gateway, state_file: Path) -> None:
    previous = HealthState.model_validate_json(state_file.read_text()) if state_file.exists() else HealthState()
    observation = gateway.observe()
    state = classify(observation, gateway.card_ssid)
    if state != previous.state:
        iface = f" (iface {observation.wifi_iface})" if observation.wifi_iface else ""
        logger.info("%s -> %s%s", previous.state or "unknown", state, iface)
    current, actions = plan(state, previous)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(current.model_dump_json())
    for action in actions:
        gateway.apply(action, current)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-ssid", required=True)
    parser.add_argument("--card-subnet", required=True, help="present in the routing table once the card's lease is up")
    parser.add_argument("--connection", required=True, help="NetworkManager profile for the card")
    parser.add_argument("--proxy-unit", required=True, help="systemd unit bridging the card")
    parser.add_argument("--driver", required=True, help="kernel module for the adapter")
    parser.add_argument("--usb-id", required=True, help="adapter's vendor:product, e.g. 0e8d:7961")
    parser.add_argument("--state-file", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="cpap-gateway-health: %(message)s")
    vendor_id, product_id = args.usb_id.split(":")
    gateway = Gateway(
        card_ssid=args.card_ssid,
        card_subnet=args.card_subnet,
        connection=args.connection,
        proxy_unit=args.proxy_unit,
        driver=args.driver,
        vendor_id=vendor_id,
        product_id=product_id,
        sysfs_usb_devices=SYSFS_USB_DEVICES,
    )
    check(gateway, args.state_file)


if __name__ == "__main__":
    main()
