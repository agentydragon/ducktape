from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator

import pytest
import pytest_bazel

from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.dbus_fast_example.client import ExampleClient
from x.dbus_fast_example.service_manager import ServiceManager


@pytest.fixture(scope="session")
def bus_address() -> Iterator[str]:
    out_dir = undeclared_outputs_dir()
    dbus_stderr = (out_dir / "dbus_daemon_stderr.log").open("w")
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        stderr=dbus_stderr,
        text=True,
    )
    assert proc.stdout
    address = proc.stdout.readline().strip()
    yield address
    proc.terminate()
    proc.wait(timeout=5)
    dbus_stderr.close()


async def test_signal_flow(bus_address: str) -> None:
    out_dir = undeclared_outputs_dir()
    svc_stdout = (out_dir / "dbus_service_stdout.log").open("w")
    svc_stderr = (out_dir / "dbus_service_stderr.log").open("w")
    manager = ServiceManager(bus_address, stdout=svc_stdout, stderr=svc_stderr)
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
    svc_stdout.close()
    svc_stderr.close()


if __name__ == "__main__":
    pytest_bazel.main()
