from pathlib import Path

import pytest
import pytest_bazel

from cpap.gateway.health import (
    ConnectCard,
    HealthState,
    Observation,
    ReauthorizeAdapter,
    ReloadDriver,
    StartProxy,
    State,
    StopProxy,
    classify,
    find_adapters,
    plan,
)

CARD = "Test CPAP card"


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (Observation(wifi_iface=None, card_connected=False, ssids=frozenset()), State.NO_WIFI_DEVICE),
        (Observation(wifi_iface="wlan0", card_connected=True, ssids=frozenset()), State.CONNECTED),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset({CARD, "x"})), State.CARD_VISIBLE),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset({"x"})), State.CARD_AWAY),
        (Observation(wifi_iface="wlan0", card_connected=False, ssids=frozenset()), State.RADIO_DEAF),
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


def test_find_adapters(tmp_path: Path) -> None:
    for name, vendor, product in [("1-1", "0e8d", "7961"), ("1-2", "0e8d", "0001"), ("2-1", "0e8d", "7961")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "idVendor").write_text(f"{vendor}\n")
        (tmp_path / name / "idProduct").write_text(f"{product}\n")
    (tmp_path / "usb1").mkdir()  # root hub interface dirs carry no ids
    assert find_adapters(tmp_path, "0e8d", "7961") == [tmp_path / "1-1", tmp_path / "2-1"]


if __name__ == "__main__":
    pytest_bazel.main()
