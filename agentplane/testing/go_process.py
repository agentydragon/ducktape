"""Run a built Go service as a child process in a test: wait until it is ready, stop it as a Pod's TERM would."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import signal
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

# The generated health stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

_DUMP_SECONDS = 5
_STARTUP_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class TcpEndpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, value: object) -> TcpEndpoint:
        if not isinstance(value, str):
            raise TypeError("TCP endpoint must be a string")
        host, separator, raw_port = value.rpartition(":")
        if not separator:
            raise ValueError(f"invalid TCP endpoint: {value!r}")
        host = host.removeprefix("[").removesuffix("]")
        address = ipaddress.ip_address(host)
        if not raw_port.isascii() or not raw_port.isdigit():
            raise ValueError(f"invalid TCP endpoint port: {value!r}")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError(f"TCP endpoint port must be in 1..65535: {value!r}")
        return cls(str(address), port)

    @property
    def target(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def url_host(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host

    @property
    def probe_target(self) -> str:
        address = ipaddress.ip_address(self.host)
        if address.is_unspecified:
            return f"127.0.0.1:{self.port}" if address.version == 4 else f"[::1]:{self.port}"
        return self.target


def startup_object(contents: bytes) -> dict[str, object]:

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate startup endpoint key: {key!r}")
            result[key] = value
        return result

    value = json.loads(contents, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("startup endpoints must be a JSON object with string keys")
    return value


@dataclass
class GoProcess[Endpoints]:
    process: asyncio.subprocess.Process
    stderr: Path
    endpoints: Endpoints
    health_target: str

    def terminate(self) -> None:
        self.process.terminate()

    async def health(self, *, serving: bool) -> None:
        """Until the readiness verdict on the gRPC health listener is `serving`; fails at once if the process died."""
        expected = health_pb2.HealthCheckResponse.SERVING if serving else health_pb2.HealthCheckResponse.NOT_SERVING
        async with grpc.aio.insecure_channel(self.health_target) as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            async for attempt in AsyncRetrying(stop=stop_after_delay(20), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    assert self.process.returncode is None, (
                        f"process exited with {self.process.returncode}:\n{await asyncio.to_thread(self.stderr.read_text)}"
                    )
                    assert (await stub.Check(health_pb2.HealthCheckRequest(), timeout=1)).status == expected
                    return
        raise AssertionError("health retry exhausted")


@asynccontextmanager
async def go_process[Endpoints](
    binary: Path,
    *,
    env: Mapping[str, str],
    stderr: Path,
    startup_file: Path,
    parse_endpoints: Callable[[bytes], Endpoints],
    health_target: Callable[[Endpoints], str],
    grace_s: float,
) -> AsyncIterator[GoProcess[Endpoints]]:
    """Run `binary` until its discovered gRPC health endpoint reports SERVING."""
    await asyncio.to_thread(startup_file.unlink, missing_ok=True)
    with stderr.open("wb") as log:
        process = await asyncio.create_subprocess_exec(binary, env=dict(env), stdout=log, stderr=log)
    successful_scope = False
    try:
        endpoints = await _wait_for_endpoints(process, stderr, startup_file, parse_endpoints)
        running = GoProcess(process, stderr, endpoints, health_target(endpoints))
        await running.health(serving=True)
        yield running
        successful_scope = True
    finally:
        cleanup = asyncio.create_task(_stop_process(process, stderr, grace_s, assert_success=successful_scope))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


async def _wait_for_endpoints[Endpoints](
    process: asyncio.subprocess.Process, stderr: Path, startup_file: Path, parse_endpoints: Callable[[bytes], Endpoints]
) -> Endpoints:
    deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            stopped = await asyncio.to_thread(stderr.read_text)
            raise AssertionError(f"process exited with {process.returncode} before publishing endpoints:\n{stopped}")
        try:
            contents = await asyncio.to_thread(startup_file.read_bytes)
        except FileNotFoundError:
            await asyncio.sleep(0.02)
            continue
        return parse_endpoints(contents)
    raise TimeoutError(
        f"process did not publish endpoints in {_STARTUP_TIMEOUT_SECONDS}s: {await asyncio.to_thread(stderr.read_text)}"
    )


async def _stop_process(
    process: asyncio.subprocess.Process, stderr: Path, grace_s: float, *, assert_success: bool
) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(grace_s):
            await process.wait()
    except TimeoutError:
        # QUIT makes a Go process dump its goroutines to stderr: what it was waiting on.
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGQUIT)
        try:
            async with asyncio.timeout(_DUMP_SECONDS):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
        stopped = await asyncio.to_thread(stderr.read_text)
        raise AssertionError(f"process did not terminate within its grace budget:\n{stopped}") from None
    if assert_success:
        assert process.returncode == 0, await asyncio.to_thread(stderr.read_text)
