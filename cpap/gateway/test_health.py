import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, create_autospec

import pytest
import pytest_bazel
from dbus_fast import Message

from cpap.gateway.health import (
    AccessPoint,
    ConnectCard,
    Gateway,
    HealthState,
    Observation,
    ReauthorizeAdapter,
    ReloadDriver,
    RestoreCard,
    StartProxy,
    State,
    StopProxy,
    SystemBus,
    classify,
    find_adapters,
    plan,
)
from cpap.gateway.restore import RestoreError

CARD = "Test CPAP card"
CARD_MAC = "02:00:00:00:00:01"


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (Observation(wifi_iface=None, card_connected=False, ssids=frozenset()), State.NO_WIFI_DEVICE),
        (Observation(wifi_iface="wlan0", card_connected=True, ssids=frozenset()), State.CONNECTED),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset({CARD, "x"})), State.CARD_VISIBLE),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset({"x"})), State.CARD_AWAY),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset()), State.RADIO_DEAF),
        (
            Observation(
                wifi_iface="wlan0", card_connected=False, ssids=frozenset({"ez Share"}), default_card_visible=True
            ),
            State.CARD_RESET,
        ),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset({"ez Share"})), State.CARD_AWAY),
    ],
)
def test_classify(observation: Observation, expected: State) -> None:
    assert classify(observation, CARD) == expected


@pytest.mark.parametrize("state", [State.CONNECTED, State.CARD_VISIBLE, State.CARD_AWAY])
def test_healthy_radio_resets_failures(state: State) -> None:
    current, _ = plan(state, HealthState(state=State.RADIO_DEAF, consecutive_failures=7))
    assert current == HealthState(state=state)


def test_card_visible_retries_association() -> None:
    assert plan(State.CARD_VISIBLE, HealthState())[1] == [StopProxy(), ConnectCard()]


def test_connected_starts_proxy() -> None:
    assert plan(State.CONNECTED, HealthState())[1] == [StartProxy()]


def test_wedged_radio_escalates_alternately() -> None:
    state = HealthState()
    recoveries = {}
    for check in range(1, 21):
        state, actions = plan(State.RADIO_DEAF, state)
        assert actions[0] == StopProxy()
        if actions[1:]:
            recoveries[check] = actions[1:]
    assert recoveries == {
        5: [ReloadDriver()],
        10: [ReauthorizeAdapter()],
        15: [ReloadDriver()],
        20: [ReauthorizeAdapter()],
    }


def test_missing_device_counts_as_failure() -> None:
    current, _ = plan(State.NO_WIFI_DEVICE, HealthState(state=State.RADIO_DEAF, consecutive_failures=3))
    assert current.consecutive_failures == 4


def test_reset_recovery_retries_with_cooldown() -> None:
    previous = HealthState(state=State.RADIO_DEAF, consecutive_failures=4)
    attempts = []
    for check in range(1, 12):
        previous, actions = plan(State.CARD_RESET, previous)
        assert actions[0] == StopProxy()
        if RestoreCard() in actions:
            attempts.append(check)
    assert attempts == [1, 6, 11]


@pytest.fixture
def gateway(tmp_path: Path) -> Gateway:
    return Gateway(
        bus=create_autospec(SystemBus, instance=True),
        card_ssid=CARD,
        card_mac=CARD_MAC,
        connection="test-card",
        psk_file=tmp_path / "wifi_password",
        admin_password_file=tmp_path / "admin_password",
        proxy_unit="test-proxy.service",
        driver="test-driver",
        vendor_id="0000",
        product_id="0000",
        sysfs_usb_devices=tmp_path,
    )


def test_default_card_requires_both_identity_and_default_ssid(gateway: Gateway) -> None:
    wrong_mac = AccessPoint(path="/ap/other", ssid="ez Share", bssid="02:00:00:00:00:02")
    normal_card = AccessPoint(path="/ap/normal", ssid=CARD, bssid=CARD_MAC)
    reset_card = AccessPoint(path="/ap/reset", ssid="ez Share", bssid=CARD_MAC.lower())
    assert gateway.default_card([wrong_mac, normal_card]) is None
    assert gateway.default_card([wrong_mac, normal_card, reset_card]) == reset_card


@pytest.mark.parametrize("interrupted", [False, True])
async def test_factory_connection_pins_card_and_cleans_up(gateway: Gateway, interrupted: bool) -> None:
    bus = create_autospec(SystemBus, instance=True)
    bus.call.side_effect = [[["/ap/reset"]], [[]], ["/profile/recovery", {}], ["/active/recovery"], [], []]
    bus.get.side_effect = [b"ez Share", CARD_MAC, 2]
    gateway = replace(gateway, bus=bus)

    async def associate() -> None:
        async with gateway.factory_connection("/device/wifi"):
            if interrupted:
                raise RestoreError("test interrupted")

    if interrupted:
        with pytest.raises(RestoreError):
            await associate()
    else:
        await associate()
    calls = bus.call.call_args_list
    settings = calls[2].args[5][0]
    assert settings["802-11-wireless"]["bssid"].value == bytes.fromhex(CARD_MAC.replace(":", ""))
    assert calls[3].args[5] == ["/profile/recovery", "/device/wifi", "/ap/reset"]
    assert calls[4].args[3] == "DeactivateConnection"
    assert calls[5].args[1:4] == ("/profile/recovery", "org.freedesktop.NetworkManager.Settings.Connection", "Delete")


async def test_factory_connection_rechecks_card_before_associating(gateway: Gateway) -> None:
    bus = create_autospec(SystemBus, instance=True)
    bus.call.return_value = [["/ap/other"]]
    bus.get.side_effect = [b"ez Share", "02:00:00:00:00:02"]
    gateway = replace(gateway, bus=bus)
    with pytest.raises(RestoreError):
        async with gateway.factory_connection("/device/wifi"):
            pytest.fail("must not connect to another card")
    assert [call.args[3] for call in bus.call.call_args_list] == ["GetAllAccessPoints"]


@pytest.mark.parametrize("state", [2, 4])
async def test_connection_state_signal_completes_or_aborts_wait(gateway: Gateway, state: int) -> None:
    bus = create_autospec(SystemBus, instance=True)
    bus.bus = Mock()
    bus.get.side_effect = [1, 1, state]
    signal = Message.new_signal(
        "/active/recovery", "org.freedesktop.NetworkManager.Connection.Active", "StateChanged", "uu", [state, 0]
    )
    bus.bus.add_message_handler.side_effect = lambda callback: asyncio.get_running_loop().call_soon(callback, signal)
    gateway = replace(gateway, bus=bus)
    if state == 4:
        with pytest.raises(RestoreError):
            await gateway.wait_for_connection("/active/recovery")
    else:
        await gateway.wait_for_connection("/active/recovery")
    assert [call.args[3] for call in bus.call.call_args_list] == ["AddMatch", "RemoveMatch"]
    bus.bus.remove_message_handler.assert_called_once_with(bus.bus.add_message_handler.call_args.args[0])


def test_find_adapters(tmp_path: Path) -> None:
    for name, vendor, product in [("1-1", "0e8d", "7961"), ("1-2", "0e8d", "0001"), ("2-1", "0e8d", "7961")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "idVendor").write_text(f"{vendor}\n")
        (tmp_path / name / "idProduct").write_text(f"{product}\n")
    (tmp_path / "usb1").mkdir()  # root hub interface dirs carry no ids
    assert find_adapters(tmp_path, "0e8d", "7961") == [tmp_path / "1-1", tmp_path / "2-1"]


if __name__ == "__main__":
    pytest_bazel.main()
