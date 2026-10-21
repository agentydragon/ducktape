from unittest.mock import AsyncMock, MagicMock, patch

import pytest_bazel
from dbus_next import Message, Variant
from dbus_next.constants import MessageType

from util.network_monitor import NetworkMonitor


def state_changed(state: int) -> Message:
    return Message.new_signal(
        "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager", "StateChanged", "u", [state]
    )


async def test_online_edges_and_cleanup() -> None:
    bus = MagicMock()
    bus.connect = AsyncMock()
    bus.call = AsyncMock(
        return_value=Message(
            message_type=MessageType.METHOD_RETURN, reply_serial=1, signature="v", body=[Variant("u", 70)]
        )
    )
    online = MagicMock()
    with patch("util.network_monitor.MessageBus", return_value=bus):
        async with NetworkMonitor(online):
            handler = bus.add_message_handler.call_args.args[0]
            online.assert_not_called()
            handler(state_changed(70))
            online.assert_not_called()
            handler(state_changed(20))
            handler(state_changed(50))  # Local connectivity is not internet recovery.
            online.assert_not_called()
            handler(state_changed(70))
            handler(state_changed(70))
            online.assert_called_once_with()
            handler(
                Message.new_signal(
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameOwnerChanged",
                    "sss",
                    ["org.freedesktop.NetworkManager", ":1.10", ""],
                )
            )
            handler(state_changed(70))
            assert online.call_count == 2
        bus.remove_message_handler.assert_called_once_with(handler)
        bus.disconnect.assert_called_once_with()


async def test_missing_network_manager_can_start_later() -> None:
    bus = MagicMock()
    bus.connect = AsyncMock()
    bus.call = AsyncMock(
        side_effect=[
            Message(message_type=MessageType.METHOD_RETURN, reply_serial=1),
            Message(message_type=MessageType.METHOD_RETURN, reply_serial=2),
            Message(
                message_type=MessageType.ERROR, reply_serial=3, error_name="org.freedesktop.DBus.Error.ServiceUnknown"
            ),
        ]
    )
    online = MagicMock()
    with patch("util.network_monitor.MessageBus", return_value=bus):
        async with NetworkMonitor(online):
            online.assert_not_called()
            bus.add_message_handler.call_args.args[0](state_changed(70))
            online.assert_called_once_with()
        bus.disconnect.assert_called_once_with()


async def test_missing_system_bus_does_not_stop_poller() -> None:
    online = MagicMock()
    with patch("util.network_monitor.MessageBus", side_effect=FileNotFoundError):
        async with NetworkMonitor(online):
            online.assert_not_called()


async def test_signal_during_snapshot_is_not_overwritten() -> None:
    bus = MagicMock()
    bus.connect = AsyncMock()

    async def call(message: Message) -> Message:
        if message.member == "Get":
            bus.add_message_handler.call_args.args[0](state_changed(20))
        return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1, signature="v", body=[Variant("u", 70)])

    bus.call = AsyncMock(side_effect=call)
    online = MagicMock()
    with patch("util.network_monitor.MessageBus", return_value=bus):
        async with NetworkMonitor(online):
            online.assert_not_called()
            bus.add_message_handler.call_args.args[0](state_changed(70))
            online.assert_called_once_with()


async def test_failed_connection_is_closed() -> None:
    bus = MagicMock()
    bus.connect = AsyncMock(side_effect=ConnectionRefusedError)
    with patch("util.network_monitor.MessageBus", return_value=bus):
        async with NetworkMonitor(MagicMock()):
            bus.disconnect.assert_called_once_with()
        bus.disconnect.assert_called_once_with()


if __name__ == "__main__":
    pytest_bazel.main()
