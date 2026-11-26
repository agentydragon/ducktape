"""Run a built Go service as a child process in a test: wait until it is ready, stop it as a Pod's TERM would."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import httpx
from grpc_health.v1 import health_pb2, health_pb2_grpc
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

# The generated health stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

_DUMP_SECONDS = 5
_READY_SECONDS = 20


@dataclass(frozen=True)
class GrpcHealth:
    """Ready once the standard gRPC health service on `port` reports SERVING."""

    port: int
    credentials: grpc.ChannelCredentials | None = None


@dataclass(frozen=True)
class HttpReadiness:
    """Ready once a GET of `url` answers 2xx."""

    url: str


type Readiness = GrpcHealth | HttpReadiness


@dataclass
class GoProcess:
    process: asyncio.subprocess.Process
    stderr: Path
    readiness: Readiness
    # Set by `kill`: the stop then expects SIGKILL's exit status instead of a clean exit.
    killed: bool = field(default=False, init=False)

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Suspend the child for a simulated outage and resume it on every exit path."""
        self.process.send_signal(signal.SIGSTOP)
        try:
            yield
        finally:
            self.process.send_signal(signal.SIGCONT)

    def terminate(self) -> None:
        self.process.terminate()

    async def kill(self) -> None:
        """Crash the child without a graceful shutdown, as an OOM kill or node loss would."""
        self.killed = True
        self.process.kill()
        assert await self.process.wait() == -signal.SIGKILL

    async def health(self, *, serving: bool) -> None:
        """Until the readiness verdict on the gRPC health listener is `serving`; fails at once if the process died."""
        if not isinstance(self.readiness, GrpcHealth):
            raise TypeError(f"process has no gRPC health listener: {self.readiness=}")
        expected = health_pb2.HealthCheckResponse.SERVING if serving else health_pb2.HealthCheckResponse.NOT_SERVING
        health = self.readiness
        channel = (
            grpc.aio.secure_channel(f"localhost:{health.port}", health.credentials)
            if health.credentials is not None
            else grpc.aio.insecure_channel(f"127.0.0.1:{health.port}")
        )
        async with channel:
            stub = health_pb2_grpc.HealthStub(channel)
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(_READY_SECONDS), wait=wait_fixed(0.05), reraise=True
            ):
                with attempt:
                    assert self.process.returncode is None, (
                        f"process exited with {self.process.returncode}:\n{await asyncio.to_thread(self.stderr.read_text)}"
                    )
                    assert (await stub.Check(health_pb2.HealthCheckRequest(), timeout=0.5)).status == expected
                    return
        raise AssertionError("health retry exhausted")

    async def _http_ready(self, url: str) -> None:
        # Never route the loopback probe through a proxy a test configured in the environment.
        async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(_READY_SECONDS),
                wait=wait_fixed(0.05),
                retry=retry_if_exception_type(httpx.HTTPError),
                reraise=True,
            ):
                with attempt:
                    assert self.process.returncode is None, (
                        f"process exited with {self.process.returncode}:\n{await asyncio.to_thread(self.stderr.read_text)}"
                    )
                    (await client.get(url)).raise_for_status()
                    return
        raise AssertionError("readiness retry exhausted")

    async def ready(self) -> None:
        if isinstance(self.readiness, GrpcHealth):
            await self.health(serving=True)
        else:
            await self._http_ready(self.readiness.url)


@asynccontextmanager
async def go_process(
    binary: Path,
    *,
    env: Mapping[str, str],
    stderr: Path,
    readiness: Readiness,
    grace_s: float,
    pass_fds: Sequence[int] = (),
) -> AsyncIterator[GoProcess]:
    """`binary` with only `env` and inherited descriptors, once `readiness` holds."""
    with stderr.open("wb") as log:
        # These test services have no collector. Avoid waiting for OTLP export during shutdown.
        process_env = {**env, "OTEL_SDK_DISABLED": "true"}
        process = await asyncio.create_subprocess_exec(
            binary, env=process_env, stdout=log, stderr=log, pass_fds=tuple(pass_fds)
        )
    running = GoProcess(process=process, stderr=stderr, readiness=readiness)
    try:
        await running.ready()
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
        assert process.returncode == (-signal.SIGKILL if running.killed else 0), await asyncio.to_thread(
            stderr.read_text
        )


type WhileStopped = Callable[[], Awaitable[None]]
type RestartGoProcess = Callable[[WhileStopped | None], Awaitable[GoProcess]]


@asynccontextmanager
async def restartable_go_process(
    process_context: Callable[[int], AbstractAsyncContextManager[GoProcess]],
) -> AsyncIterator[tuple[GoProcess, RestartGoProcess]]:
    """Start a process and provide a restart that runs a callback during the outage."""
    generation = 0
    async with AsyncExitStack() as stack:
        started = await stack.enter_async_context(process_context(generation))

        async def restart(while_stopped: WhileStopped | None) -> GoProcess:
            nonlocal generation
            await stack.aclose()
            if while_stopped is not None:
                await while_stopped()
            generation += 1
            return await stack.enter_async_context(process_context(generation))

        yield started, restart
