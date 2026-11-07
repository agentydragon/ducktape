import asyncio
import threading
from collections.abc import Iterator

import httpx
import pytest_bazel

from devinfra.x.firecracker.manager.service import _control_client, _firecracker_client, _require_auth, app


async def test_health_responds_while_boot_waits_for_firecracker() -> None:
    started = threading.Event()
    release = threading.Event()
    deadline = threading.Timer(5, release.set)
    boots: list[str] = []

    class BlockingFirecracker:
        def wait_ready(self) -> None:
            deadline.start()
            started.set()
            release.wait()

        def boot(self, **kwargs: str) -> None:
            boots.append(kwargs["kernel_path"])

    class ReadyControl:
        def wait_ready(self) -> None:
            pass

    def firecracker_client() -> Iterator[BlockingFirecracker]:
        yield BlockingFirecracker()

    def control_client() -> Iterator[ReadyControl]:
        yield ReadyControl()

    app.dependency_overrides[_require_auth] = lambda: None
    app.dependency_overrides[_firecracker_client] = firecracker_client
    app.dependency_overrides[_control_client] = control_client
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            boot_task = asyncio.create_task(client.post("/vms/test-vm/boot"))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                health = await client.get("/health")
                assert health.json() == {"status": "ok"}
                assert not release.is_set()
            finally:
                release.set()
                boot_response = await boot_task
                deadline.cancel()

        assert boot_response.json() == {"status": "booted", "vm_id": "test-vm"}
        assert len(boots) == 1
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    pytest_bazel.main()
