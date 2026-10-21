"""Best-effort NetworkManager recovery notifications for desktop pollers."""

import asyncio
import logging
from collections.abc import Callable
from types import TracebackType
from typing import Self

from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, MessageType
from dbus_next.errors import DBusError

logger = logging.getLogger(__name__)

_NETWORK_MANAGER = "org.freedesktop.NetworkManager"
_NETWORK_MANAGER_PATH = "/org/freedesktop/NetworkManager"
_DBUS = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
# NM_STATE_CONNECTED_GLOBAL: includes installations with connectivity checking disabled.
_CONNECTED_GLOBAL = 70


class NetworkMonitor:
    """Notify on entry into NetworkManager's globally connected state.

    The initial snapshot never triggers a callback; callers do their own initial sync.
    This is a hint to retry, not proof an individual endpoint is reachable. Callers
    must retain timed retries when the system bus or NetworkManager is unavailable.
    """

    def __init__(self, on_online: Callable[[], None]) -> None:
        self._on_online = on_online
        self._online = False
        self._saw_state_signal = False
        self._bus: MessageBus | None = None

    async def __aenter__(self) -> Self:
        try:
            async with asyncio.timeout(5):
                self._bus = MessageBus(bus_type=BusType.SYSTEM)
                try:
                    await self._bus.connect()
                except BaseException:
                    self._close()
                    raise
                self._bus.add_message_handler(self._handle_message)
                for rule in (
                    f"type='signal',sender='{_NETWORK_MANAGER}',interface='{_NETWORK_MANAGER}',"
                    f"path='{_NETWORK_MANAGER_PATH}',member='StateChanged'",
                    f"type='signal',sender='{_DBUS}',interface='{_DBUS}',"
                    f"path='{_DBUS_PATH}',member='NameOwnerChanged',arg0='{_NETWORK_MANAGER}'",
                ):
                    await self._call(
                        Message(
                            destination=_DBUS,
                            path=_DBUS_PATH,
                            interface=_DBUS,
                            member="AddMatch",
                            signature="s",
                            body=[rule],
                        )
                    )
                reply = await self._call(
                    Message(
                        destination=_NETWORK_MANAGER,
                        path=_NETWORK_MANAGER_PATH,
                        interface="org.freedesktop.DBus.Properties",
                        member="Get",
                        signature="ss",
                        body=[_NETWORK_MANAGER, "State"],
                    )
                )
                # A signal received during Get is newer than its snapshot.
                if not self._saw_state_signal:
                    self._online = reply.body[0].value == _CONNECTED_GLOBAL
        except OSError, EOFError, DBusError, TimeoutError:
            # Keep any installed matches: NetworkManager may start later.
            logger.warning("Network recovery monitoring unavailable; retaining scheduled retries", exc_info=True)
        except BaseException:
            self._close()
            raise
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._close()

    def _close(self) -> None:
        if self._bus is not None:
            self._bus.remove_message_handler(self._handle_message)
            # Disconnecting the dedicated connection also removes its match rules.
            self._bus.disconnect()
            self._bus = None

    async def _call(self, message: Message) -> Message:
        assert self._bus is not None
        reply = await self._bus.call(message)
        if reply is None:
            raise EOFError("NetworkManager D-Bus call returned no reply")
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name or "org.freedesktop.DBus.Error.Failed", str(reply.body))
        return reply

    def _handle_message(self, message: Message) -> None:
        if message.message_type != MessageType.SIGNAL:
            return
        if (
            message.interface == _DBUS
            and message.path == _DBUS_PATH
            and message.member == "NameOwnerChanged"
            and message.body[0] == _NETWORK_MANAGER
        ):
            self._online = False
        elif (
            message.interface == _NETWORK_MANAGER
            and message.path == _NETWORK_MANAGER_PATH
            and message.member == "StateChanged"
        ):
            self._saw_state_signal = True
            online = message.body[0] == _CONNECTED_GLOBAL
            was_online = self._online
            self._online = online
            if online and not was_online:
                self._on_online()
