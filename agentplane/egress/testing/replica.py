"""Separate processes are essential: mitmproxy's ctx singleton cannot represent two masters."""

import asyncio
import multiprocessing
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from pathlib import Path
from textwrap import dedent

import aiohttp
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.egress.main import Settings, async_main


def _run(settings: Settings) -> None:
    asyncio.run(async_main(settings))


@dataclass
class Replica:
    proxy_port: int
    admin_port: int
    process: BaseProcess

    async def health(self, status: int) -> None:
        async with aiohttp.ClientSession() as client:
            async for attempt in AsyncRetrying(stop=stop_after_delay(20), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    assert self.process.is_alive(), f"proxy exited with {self.process.exitcode}"
                    async with client.get(f"http://127.0.0.1:{self.admin_port}/healthz") as response:
                        assert response.status == status
                        await response.read()
                        return
        raise AssertionError("health retry exhausted")


@asynccontextmanager
async def replica(settings: Settings) -> AsyncIterator[Replica]:
    process = multiprocessing.get_context("spawn").Process(target=_run, args=(settings,))
    process.start()
    running = Replica(settings.listen_port, settings.admin_port, process)
    try:
        await running.health(200)
        yield running
    finally:
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 55)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join)
            raise AssertionError("proxy did not terminate within its grace budget")
        assert process.exitcode == 0
        process.close()


def kubeconfig(path: Path, api_port: int) -> Path:
    path.write_text(
        dedent(f"""apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: http://127.0.0.1:{api_port}
contexts:
- name: test
  context:
    cluster: test
    user: test
current-context: test
users:
- name: test
  user: {{}}
""")
    )
    return path
