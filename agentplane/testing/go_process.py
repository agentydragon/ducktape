"""Run a built Go service as a child process in a test: wait until it is ready, stop it as a Pod's TERM would."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

# The generated health stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

_DUMP_SECONDS = 5


@dataclass
class GoProcess:
    process: asyncio.subprocess.Process
    stderr: Path
    health_port: int

    def terminate(self) -> None:
        self.process.terminate()

    async def health(self, *, serving: bool) -> None:
        """Until the readiness verdict on the gRPC health listener is `serving`; fails at once if the process died."""
        expected = health_pb2.HealthCheckResponse.SERVING if serving else health_pb2.HealthCheckResponse.NOT_SERVING
        async with grpc.aio.insecure_channel(f"127.0.0.1:{self.health_port}") as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            async for attempt in AsyncRetrying(stop=stop_after_delay(20), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    assert self.process.returncode is None, (
                        f"process exited with {self.process.returncode}:\n{await asyncio.to_thread(self.stderr.read_text)}"
                    )
                    assert (await stub.Check(health_pb2.HealthCheckRequest(), timeout=0.5)).status == expected
                    return
        raise AssertionError("health retry exhausted")


@asynccontextmanager
async def go_process(
    binary: Path,
    *,
    env: Mapping[str, str],
    stderr: Path,
    health_port: int,
    grace_s: float,
    pass_fds: Sequence[int] = (),
) -> AsyncIterator[GoProcess]:
    """`binary` with only `env` and inherited descriptors, once it reports SERVING on `health_port`."""
    with stderr.open("wb") as log:
        # These test services have no collector. Avoid waiting for OTLP export during shutdown.
        process_env = {**env, "OTEL_SDK_DISABLED": "true"}
        process = await asyncio.create_subprocess_exec(
            binary, env=process_env, stdout=log, stderr=log, pass_fds=tuple(pass_fds)
        )
    running = GoProcess(process, stderr, health_port)
    try:
        await running.health(serving=True)
        yield running
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            async with asyncio.timeout(grace_s):
                await process.wait()
        except TimeoutError:
            # QUIT makes a Go process dump its goroutines to stderr: what it was waiting on.
            process.send_signal(signal.SIGQUIT)
            try:
                async with asyncio.timeout(_DUMP_SECONDS):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
            stopped = await asyncio.to_thread(stderr.read_text)
            raise AssertionError(f"process did not terminate within its grace budget:\n{stopped}") from None
        assert process.returncode == 0, await asyncio.to_thread(stderr.read_text)
