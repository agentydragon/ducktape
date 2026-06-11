from __future__ import annotations

import asyncio
import sys

from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal


class ExampleService(ServiceInterface):
    """A minimal service exposing a method and signal."""

    def __init__(self) -> None:
        super().__init__("org.example.TestInterface")
        self.stop_event = asyncio.Event()

    @method()
    def Ping(self) -> str:  # noqa: N802 - DBus method name
        return "pong"

    @method()
    def EmitSignal(self, msg: str) -> None:  # noqa: N802 - DBus method name
        self.Notify(msg)

    @method()
    def Quit(self) -> None:  # noqa: N802 - DBus method name
        self.stop_event.set()

    @signal()
    def Notify(self, msg: str) -> str:  # noqa: N802 - DBus signal name
        return msg


async def main(bus_address: str) -> None:
    bus = await MessageBus(bus_address=bus_address).connect()
    service = ExampleService()
    bus.export("/org/example/TestObject", service)
    await bus.request_name("org.example.TestService")
    await service.stop_event.wait()
    bus.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
