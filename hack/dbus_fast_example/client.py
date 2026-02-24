from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dbus_fast.aio import MessageBus


class ExampleClient:
    """Client that subscribes to Notify signals."""

    def __init__(self, bus_address: str) -> None:
        self.bus_address = bus_address
        self.bus: MessageBus | None = None
        self.interface: Any = None

    async def connect(self) -> None:
        self.bus = await MessageBus(bus_address=self.bus_address).connect()
        introspection = await self.bus.introspect("org.example.TestService", "/org/example/TestObject")
        obj = self.bus.get_proxy_object("org.example.TestService", "/org/example/TestObject", introspection)
        self.interface = obj.get_interface("org.example.TestInterface")

    async def disconnect(self) -> None:
        if self.bus:
            self.bus.disconnect()
            self.bus = None

    def on_notify(self, cb: Callable[[str], None]) -> None:
        assert self.interface
        self.interface.on_notify(cb)

    def off_notify(self, cb: Callable[[str], None]) -> None:
        assert self.interface
        self.interface.off_notify(cb)
