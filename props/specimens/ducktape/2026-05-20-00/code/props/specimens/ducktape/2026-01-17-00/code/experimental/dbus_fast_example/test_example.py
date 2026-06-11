from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator

import pytest

from experimental.dbus_fast_example.client import ExampleClient
from experimental.dbus_fast_example.service_manager import ServiceManager


@pytest.fixture(scope="session")
def bus_address() -> Iterator[str]:
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout
    address = proc.stdout.readline().strip()
    yield address
    proc.terminate()
    proc.wait(timeout=5)


async def test_signal_flow(bus_address: str) -> None:
    manager = ServiceManager(bus_address)
    await manager.start()

    client = ExampleClient(bus_address)
    await client.connect()

    received: list[str] = []

    def handler(msg: str) -> None:
        received.append(msg)

    client.on_notify(handler)

    await manager.emit("hello")
    await asyncio.sleep(0.1)

    assert received == ["hello"]

    client.off_notify(handler)
    await manager.emit("bye")
    await asyncio.sleep(0.1)

    assert received == ["hello"]

    assert client.bus
    first_unique = client.bus._name_owners["org.example.TestService"]
    await manager.stop()
    await manager.start()
    assert client.bus
    second_unique = client.bus._name_owners["org.example.TestService"]
    assert first_unique != second_unique

    client.on_notify(handler)
    await manager.emit("again")
    await asyncio.sleep(0.1)

    assert received[-1] == "again"

    await client.disconnect()
    await manager.stop()
