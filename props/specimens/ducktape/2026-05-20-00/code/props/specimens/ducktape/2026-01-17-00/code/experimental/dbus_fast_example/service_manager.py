from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dbus_fast.aio import MessageBus
from dbus_fast.message import Message


class ServiceManager:
    """Launch and control the example service in a subprocess."""

    def __init__(self, bus_address: str) -> None:
        self.bus_address = bus_address
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if self.proc:
            raise RuntimeError("service already running")
        script = Path(__file__).with_name("dbus_service.py")
        # Using asyncio.create_subprocess_exec for async subprocess
        self.proc = await asyncio.create_subprocess_exec(sys.executable, str(script), self.bus_address)
        await self._wait_until_running()

    async def _wait_until_running(self) -> None:
        bus = await MessageBus(bus_address=self.bus_address).connect()
        while True:
            try:
                msg = Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="NameHasOwner",
                    signature="s",
                    body=["org.example.TestService"],
                )
                reply = await bus.call(msg)
                if reply and reply.body[0]:
                    break
            except Exception:
                await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(0.05)
        bus.disconnect()

    async def emit(self, msg: str) -> None:
        bus = await MessageBus(bus_address=self.bus_address).connect()
        introspection = await bus.introspect("org.example.TestService", "/org/example/TestObject")
        obj = bus.get_proxy_object("org.example.TestService", "/org/example/TestObject", introspection)
        # dbus-fast dynamically generates proxy methods from introspection XML
        interface = obj.get_interface("org.example.TestInterface")
        await interface.call_emit_signal(msg)  # type: ignore[attr-defined]
        bus.disconnect()

    async def stop(self) -> None:
        if not self.proc:
            return
        bus = await MessageBus(bus_address=self.bus_address).connect()
        introspection = await bus.introspect("org.example.TestService", "/org/example/TestObject")
        obj = bus.get_proxy_object("org.example.TestService", "/org/example/TestObject", introspection)
        # dbus-fast dynamically generates proxy methods from introspection XML
        interface = obj.get_interface("org.example.TestInterface")
        await interface.call_quit()  # type: ignore[attr-defined]
        bus.disconnect()
        await asyncio.wait_for(self.proc.wait(), timeout=2)
        self.proc = None
