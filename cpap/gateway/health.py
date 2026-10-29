"""Keeps the CPAP gateway VM connected to the card and its USB WiFi adapter alive.

Runs once per invocation (a systemd timer fires it every minute): ensure the card's
NetworkManager profile exists, observe the adapter, classify the gateway, and act.
NetworkManager and systemd are driven over D-Bus. Transitions and recovery steps are
logged to stderr, which journald forwards to the serial console and so to Loki
(`container="guest-console-log"`).
"""

import argparse
import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from pydantic import BaseModel

from cpap.gateway.restore import DEFAULT_SSID, DEFAULT_WIFI_PASSWORD, RestoreError, restore_card

logger = logging.getLogger(__name__)

SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")
# The first escalation waits this many failed checks, giving NetworkManager time after
# boot or after the previous reset; later escalations follow at the same interval.
ESCALATION_INTERVAL = 5
# A healthy MT7921U scan completes in a few seconds.
SCAN_TIMEOUT_SECONDS = 30

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
NM_DEVICE = f"{NM}.Device"
NM_WIRELESS = f"{NM}.Device.Wireless"
NM_ACTIVE = f"{NM}.Connection.Active"
PROPERTIES = "org.freedesktop.DBus.Properties"
SYSTEMD = "org.freedesktop.systemd1"


class NMDeviceType(IntEnum):
    WIFI = 2


class NMActiveConnectionState(IntEnum):
    ACTIVATED = 2
    DEACTIVATED = 4


# NM_SETTINGS_ADD_CONNECTION2_FLAG_IN_MEMORY: the PSK never reaches the guest's disk.
NM_ADD_CONNECTION_IN_MEMORY = 0x2


class State(StrEnum):
    CONNECTED = "connected"
    # The card's AP is in the scan but not associated: retry the connection.
    CARD_VISIBLE = "card-visible"
    CARD_RESET = "card-reset"
    # The radio sees other networks but not the card. Expected while the CPAP travels.
    CARD_AWAY = "card-away"
    # No WiFi device, or a scan that sees no network at all: the adapter is wedged.
    NO_WIFI_DEVICE = "no-wifi-device"
    RADIO_DEAF = "radio-deaf"


class HealthState(BaseModel):
    """Persisted across invocations in a tmpfs, so a reboot starts from scratch."""

    state: State | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class Observation:
    wifi_iface: str | None
    card_connected: bool
    # Scan results; empty for a deaf radio. Not taken when there is no interface or the
    # card is already connected.
    ssids: frozenset[str]
    default_card_visible: bool = False


@dataclass(frozen=True)
class AccessPoint:
    path: str
    ssid: str
    bssid: str


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
class RestoreCard:
    pass


@dataclass(frozen=True)
class ReloadDriver:
    pass


@dataclass(frozen=True)
class ReauthorizeAdapter:
    pass


Action = StartProxy | StopProxy | ConnectCard | RestoreCard | ReloadDriver | ReauthorizeAdapter


def classify(observation: Observation, card_ssid: str) -> State:
    if observation.wifi_iface is None:
        return State.NO_WIFI_DEVICE
    if observation.card_connected:
        return State.CONNECTED
    if card_ssid in observation.ssids:
        return State.CARD_VISIBLE
    if observation.default_card_visible:
        return State.CARD_RESET
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
    if state == State.CARD_RESET:
        failures = previous.consecutive_failures + 1 if previous.state == state else 1
        return HealthState(state=state, consecutive_failures=failures), [
            StopProxy(),
            *([RestoreCard()] if failures % ESCALATION_INTERVAL == 1 else []),
        ]
    failures = previous.consecutive_failures + 1
    actions: list[Action] = [StopProxy()]
    if failures % ESCALATION_INTERVAL == 0:
        # Alternate: a driver reload first, a USB re-authorization next time round.
        actions.append(ReloadDriver() if failures % (2 * ESCALATION_INTERVAL) else ReauthorizeAdapter())
    return HealthState(state=state, consecutive_failures=failures), actions


def card_connection_settings(name: str, ssid: str, psk: str) -> dict[str, dict[str, Variant]]:
    return {
        "connection": {
            "id": Variant("s", name),
            "type": Variant("s", "802-11-wireless"),
            "autoconnect": Variant("b", True),
            # Retry forever: the card is away whenever the CPAP travels.
            "autoconnect-retries": Variant("i", 0),
        },
        "802-11-wireless": {"ssid": Variant("ay", ssid.encode()), "mode": Variant("s", "infrastructure")},
        "802-11-wireless-security": {"key-mgmt": Variant("s", "wpa-psk"), "psk": Variant("s", psk)},
        # The card is a dead end; the VM's default route stays on the pod network.
        "ipv4": {"method": Variant("s", "auto"), "never-default": Variant("b", True)},
        "ipv6": {"method": Variant("s", "disabled")},
    }


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
class SystemBus:
    bus: MessageBus

    async def call(
        self, destination: str, path: str, interface: str, member: str, signature: str = "", body: Sequence[Any] = ()
    ) -> list[Any]:
        reply = await self.bus.call(
            Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=list(body),
            )
        )
        assert reply is not None  # only a NO_REPLY_EXPECTED call returns None
        if reply.message_type == MessageType.ERROR:
            assert reply.error_name is not None  # every ERROR message names its error
            raise DBusError(reply.error_name, reply.body[0] if reply.body else "", reply)
        return list(reply.body)

    async def get(self, destination: str, path: str, interface: str, prop: str) -> Any:
        [value] = await self.call(destination, path, PROPERTIES, "Get", "ss", [interface, prop])
        return value.value


@asynccontextmanager
async def system_bus() -> AsyncIterator[SystemBus]:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        yield SystemBus(bus)
    finally:
        bus.disconnect()


@dataclass(frozen=True)
class Gateway:
    bus: SystemBus
    card_ssid: str
    connection: str
    psk_file: Path
    admin_password_file: Path
    card_mac: str
    proxy_unit: str
    driver: str
    vendor_id: str
    product_id: str
    sysfs_usb_devices: Path

    async def ensure_connection(self) -> str:
        """The card profile's D-Bus path, adding it first if NetworkManager lacks it."""
        paths: list[str]
        [paths] = await self.bus.call(NM, NM_SETTINGS_PATH, f"{NM}.Settings", "ListConnections")
        for path in paths:
            [settings] = await self.bus.call(NM, path, f"{NM}.Settings.Connection", "GetSettings")
            if settings["connection"]["id"].value == self.connection:
                return path
        settings = card_connection_settings(self.connection, self.card_ssid, self.psk_file.read_text().strip())
        added: str
        added, _ = await self.bus.call(
            NM,
            NM_SETTINGS_PATH,
            f"{NM}.Settings",
            "AddConnection2",
            "a{sa{sv}}ua{sv}",
            [settings, NM_ADD_CONNECTION_IN_MEMORY, {}],
        )
        logger.info("added NetworkManager profile %s", self.connection)
        return added

    async def wifi_device(self) -> str | None:
        devices: list[str]
        [devices] = await self.bus.call(NM, NM_PATH, NM, "GetDevices")
        for device in devices:
            if await self.bus.get(NM, device, NM_DEVICE, "DeviceType") == NMDeviceType.WIFI:
                return device
        return None

    async def observe(self, device: str | None) -> Observation:
        if device is None:
            return Observation(wifi_iface=None, card_connected=False, ssids=frozenset())
        iface = await self.bus.get(NM, device, NM_DEVICE, "Interface")
        active = await self.bus.get(NM, device, NM_DEVICE, "ActiveConnection")
        if active != "/" and (
            await self.bus.get(NM, active, NM_ACTIVE, "Id") == self.connection
            and await self.bus.get(NM, active, NM_ACTIVE, "State") == NMActiveConnectionState.ACTIVATED
        ):
            return Observation(wifi_iface=iface, card_connected=True, ssids=frozenset())
        access_points = await self.scan(device)
        return Observation(
            wifi_iface=iface,
            card_connected=False,
            ssids=frozenset(ap.ssid for ap in access_points if ap.ssid),
            default_card_visible=self.default_card(access_points) is not None,
        )

    def default_card(self, access_points: Sequence[AccessPoint]) -> AccessPoint | None:
        return next(
            (ap for ap in access_points if ap.ssid == DEFAULT_SSID and ap.bssid.upper() == self.card_mac.upper()), None
        )

    async def scan(self, device: str) -> list[AccessPoint]:
        """SSIDs from a fresh scan; empty when the scan does not complete."""
        scanned = asyncio.Event()

        def on_message(message: Message) -> None:
            if (
                message.member == "PropertiesChanged"
                and message.path == device
                and message.body[0] == NM_WIRELESS
                and "LastScan" in message.body[1]
            ):
                scanned.set()

        rule = f"type='signal',interface='{PROPERTIES}',member='PropertiesChanged',path='{device}'"
        await self.bus.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule]
        )
        self.bus.bus.add_message_handler(on_message)
        try:
            try:
                await self.bus.call(NM, device, NM_WIRELESS, "RequestScan", "a{sv}", [{}])
            except DBusError as e:
                # Refused while a scan is already running; that scan's end still counts.
                logger.warning("RequestScan on %s refused: %s", device, e.text)
            async with asyncio.timeout(SCAN_TIMEOUT_SECONDS):
                await scanned.wait()
        except TimeoutError:
            logger.warning("WiFi scan did not complete within %ds", SCAN_TIMEOUT_SECONDS)
            return []
        finally:
            self.bus.bus.remove_message_handler(on_message)
        [access_points] = await self.bus.call(NM, device, NM_WIRELESS, "GetAllAccessPoints")
        return await self.read_access_points(access_points)

    async def read_access_points(self, paths: Sequence[str]) -> list[AccessPoint]:
        return [
            AccessPoint(
                path=path,
                ssid=bytes(await self.bus.get(NM, path, f"{NM}.AccessPoint", "Ssid")).decode(errors="replace"),
                bssid=await self.bus.get(NM, path, f"{NM}.AccessPoint", "HwAddress"),
            )
            for path in paths
        ]

    async def wait_for_connection(self, active: str) -> None:
        if await self.bus.get(NM, active, NM_ACTIVE, "State") == NMActiveConnectionState.ACTIVATED:
            return
        changed = asyncio.Event()

        def on_message(message: Message) -> None:
            if message.path == active and message.interface == NM_ACTIVE and message.member == "StateChanged":
                changed.set()

        rule = f"type='signal',interface='{NM_ACTIVE}',member='StateChanged',path='{active}'"
        await self.bus.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule]
        )
        self.bus.bus.add_message_handler(on_message)
        try:
            async with asyncio.timeout(30):
                while True:
                    changed.clear()
                    state = await self.bus.get(NM, active, NM_ACTIVE, "State")
                    if state == NMActiveConnectionState.ACTIVATED:
                        return
                    if state == NMActiveConnectionState.DEACTIVATED:
                        raise RestoreError("factory WiFi association failed")
                    await changed.wait()
        finally:
            self.bus.bus.remove_message_handler(on_message)
            await self.bus.call(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RemoveMatch", "s", [rule]
            )

    @asynccontextmanager
    async def factory_connection(self, device: str) -> AsyncIterator[None]:
        [paths] = await self.bus.call(NM, device, NM_WIRELESS, "GetAllAccessPoints")
        ap = self.default_card(await self.read_access_points(paths))
        if ap is None:
            raise RestoreError("default card is no longer visible")
        name = f"{self.connection}-factory-recovery"
        [profiles] = await self.bus.call(NM, NM_SETTINGS_PATH, f"{NM}.Settings", "ListConnections")
        for profile in profiles:
            [existing] = await self.bus.call(NM, profile, f"{NM}.Settings.Connection", "GetSettings")
            if existing["connection"]["id"].value == name:
                await self.bus.call(NM, profile, f"{NM}.Settings.Connection", "Delete")
        settings = card_connection_settings(name, DEFAULT_SSID, DEFAULT_WIFI_PASSWORD)
        settings["connection"]["autoconnect"] = Variant("b", False)
        settings["802-11-wireless"]["bssid"] = Variant("ay", bytes.fromhex(self.card_mac.replace(":", "")))
        profile, _ = await self.bus.call(
            NM,
            NM_SETTINGS_PATH,
            f"{NM}.Settings",
            "AddConnection2",
            "a{sa{sv}}ua{sv}",
            [settings, NM_ADD_CONNECTION_IN_MEMORY, {}],
        )
        active = None
        try:
            [active] = await self.bus.call(NM, NM_PATH, NM, "ActivateConnection", "ooo", [profile, device, ap.path])
            await self.wait_for_connection(active)
            yield
        finally:
            if active is not None:
                # Applying the card settings disconnects this connection first.
                with suppress(DBusError):
                    await self.bus.call(NM, NM_PATH, NM, "DeactivateConnection", "o", [active])
            await self.bus.call(NM, profile, f"{NM}.Settings.Connection", "Delete")

    async def restore_card_settings(self, device: str, connection: str) -> None:
        try:
            async with self.factory_connection(device):
                await asyncio.to_thread(
                    restore_card,
                    base_url="http://192.168.4.1",
                    expected_mac=self.card_mac,
                    ssid=self.card_ssid,
                    wifi_password=self.psk_file.read_text().strip(),
                    admin_password=self.admin_password_file.read_text().strip(),
                )
            logger.info("restored card SSID and stored WiFi/admin credentials")
        except (RestoreError, DBusError, TimeoutError, OSError) as error:
            logger.warning("card settings recovery failed (%s)", type(error).__name__)
        await self.bus.call(NM, NM_PATH, NM, "ActivateConnection", "ooo", [connection, device, "/"])

    async def apply(self, action: Action, state: HealthState, device: str | None, connection: str) -> None:
        match action:
            case StartProxy():
                await self.bus.call(
                    SYSTEMD,
                    "/org/freedesktop/systemd1",
                    f"{SYSTEMD}.Manager",
                    "StartUnit",
                    "ss",
                    [self.proxy_unit, "replace"],
                )
            case StopProxy():
                await self.bus.call(
                    SYSTEMD,
                    "/org/freedesktop/systemd1",
                    f"{SYSTEMD}.Manager",
                    "StopUnit",
                    "ss",
                    [self.proxy_unit, "replace"],
                )
            case ConnectCard():
                assert device is not None  # card-visible implies a WiFi device
                try:
                    await self.bus.call(NM, NM_PATH, NM, "ActivateConnection", "ooo", [connection, device, "/"])
                except DBusError as e:
                    # Fails while the card is out of range or still booting; the next check retries.
                    logger.warning("activating %s failed: %s", self.connection, e.text)
            case RestoreCard():
                assert device is not None
                await self.restore_card_settings(device, connection)
            case ReloadDriver():
                self._log_adapters()
                logger.info("%s for %d checks; reloading %s", state.state, state.consecutive_failures, self.driver)
                # Removal fails when the module is already gone; loading it is what matters.
                removal = await asyncio.create_subprocess_exec(
                    "modprobe", "-r", self.driver, stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await removal.communicate()
                if removal.returncode:
                    logger.warning("modprobe -r %s failed: %s", self.driver, stderr.decode().strip())
                load = await asyncio.create_subprocess_exec("modprobe", self.driver)
                if await load.wait():
                    raise subprocess.CalledProcessError(load.returncode or 0, ["modprobe", self.driver])
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


def load_state(state_file: Path) -> HealthState:
    return HealthState.model_validate_json(state_file.read_text()) if state_file.exists() else HealthState()


def save_state(state_file: Path, state: HealthState) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(state.model_dump_json())


async def check(gateway: Gateway, state_file: Path) -> None:
    previous = await asyncio.to_thread(load_state, state_file)
    connection = await gateway.ensure_connection()
    device = await gateway.wifi_device()
    observation = await gateway.observe(device)
    state = classify(observation, gateway.card_ssid)
    if state != previous.state:
        iface = f" (iface {observation.wifi_iface})" if observation.wifi_iface else ""
        logger.info("%s -> %s%s", previous.state or "unknown", state, iface)
    current, actions = plan(state, previous)
    # Saved before acting, so a failing recovery still advances the escalation count.
    await asyncio.to_thread(save_state, state_file, current)
    for action in actions:
        await gateway.apply(action, current, device, connection)


async def async_main(args: argparse.Namespace) -> None:
    vendor_id, product_id = args.usb_id.split(":")
    async with system_bus() as bus:
        gateway = Gateway(
            bus=bus,
            card_ssid=args.card_ssid,
            connection=args.connection,
            psk_file=args.psk_file,
            admin_password_file=args.admin_password_file,
            card_mac=args.card_mac,
            proxy_unit=args.proxy_unit,
            driver=args.driver,
            vendor_id=vendor_id,
            product_id=product_id,
            sysfs_usb_devices=SYSFS_USB_DEVICES,
        )
        await check(gateway, args.state_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-ssid", required=True)
    parser.add_argument("--psk-file", required=True, type=Path, help="the card's WPA passphrase")
    parser.add_argument("--admin-password-file", required=True, type=Path)
    parser.add_argument("--card-mac", required=True, help="the card's colon-separated WiFi MAC address")
    parser.add_argument("--connection", required=True, help="NetworkManager profile name for the card")
    parser.add_argument("--proxy-unit", required=True, help="systemd unit bridging the card")
    parser.add_argument("--driver", required=True, help="kernel module for the adapter")
    parser.add_argument("--usb-id", required=True, help="adapter's vendor:product, e.g. 0e8d:7961")
    parser.add_argument("--state-file", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="cpap-gateway-health: %(message)s")
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
