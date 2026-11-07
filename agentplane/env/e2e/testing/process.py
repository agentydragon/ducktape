"""Own a real Go runner subprocess and observe it through its public protocol."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Self

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess, pid1_container, wait_for_exit
from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_log_pb2, session_pb2


class RunningRunner:
    def __init__(
        self,
        config: RunnerTestConfig,
        gateway: Gateway,
        *,
        history: HistoryServiceClient,
        ingress_endpoint: Endpoint,
        control_endpoint: EnvironmentRuntimeControl | None = None,
        observe_control: bool = True,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.history = history
        self.control_endpoint = control_endpoint or gateway.endpoint
        self.observe_control = observe_control
        self.ingress_endpoint = ingress_endpoint
        self.process: RunnerProcess

    @property
    def state_dir(self) -> Path:
        return self.config.state_dir

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, _exc_type: object, exc: BaseException | None, _tb: object) -> None:
        try:
            await self.stop()
        except BaseException as cleanup_error:
            if exc is not None:
                raise BaseExceptionGroup("Go runner body and shutdown failed", [exc, cleanup_error]) from None
            raise

    async def start(self) -> None:
        self.model_config_context = model_config()
        model_endpoint = await self.model_config_context.__aenter__()
        try:
            await self._start(model_endpoint)
        except BaseException:
            await self.model_config_context.__aexit__(None, None, None)
            raise

    async def _start(self, model_endpoint: Endpoint) -> None:
        if self.observe_control:
            await self.gateway.control.disconnect()
        environment = dict(self.config.harness_environment)
        environment.update(
            {key: os.environ[key] for key in ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE") if key in os.environ}
        )
        environment["OTEL_SDK_DISABLED"] = "true"
        environment["PATH"] = "/usr/bin:/bin"
        if self.config.claude is not None:
            environment["ANTHROPIC_AUTH_TOKEN"] = self.config.claude.token
        if self.config.codex is not None:
            environment["OPENAI_API_KEY"] = self.config.codex.token
        command = launches.command(
            self.config,
            control=self.control_endpoint,
            ingress=self.ingress_endpoint,
            model_config_service=model_endpoint,
        )
        container = await asyncio.to_thread(pid1_container, command, self.state_dir.parent, environment)
        await asyncio.to_thread(container.__enter__)
        self.process = RunnerProcess(container)
        if not self.observe_control:
            return
        connected = asyncio.create_task(
            self.gateway.control.connect(self.control_endpoint, self.config.launch.sandbox.uid)
        )
        exited = asyncio.create_task(self.process.wait())
        try:
            done, _ = await asyncio.wait((connected, exited), return_when=asyncio.FIRST_COMPLETED)
            if exited in done:
                raise RuntimeError(f"Go runner exited with {exited.result()}; see container logs")
            await connected
        except BaseException:
            try:
                await self.process.stop()
            finally:
                await self.process.close()
            raise
        finally:
            connected.cancel()
            exited.cancel()
            await asyncio.gather(connected, exited, return_exceptions=True)

    async def stop(self) -> None:
        try:
            await self.process.stop()
        finally:
            try:
                await self.process.close()
            finally:
                await self.model_config_context.__aexit__(None, None, None)
        if self.observe_control:
            await self.gateway.control.disconnect()

    async def restart(
        self, *, control_endpoint: EnvironmentRuntimeControl | None = None, ingress_endpoint: Endpoint | None = None
    ) -> None:
        await self.stop()
        if control_endpoint is not None:
            self.control_endpoint = control_endpoint
        if ingress_endpoint is not None:
            self.ingress_endpoint = ingress_endpoint
        await self.start()

    async def control(self) -> environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub:
        if not self.observe_control:
            raise RuntimeError("Runner control calls are owned by the external Gateway in this test")
        return await self.gateway.control.runner()

    async def history_entries(self, session_id: str, after: int = 0) -> list[event_log_pb2.EventEntry]:
        """Read the runner's committed entries back from its configured History Service."""
        entries: list[event_log_pb2.EventEntry] = []
        cursor = after
        while True:
            page = await self.history.read_session_events(session_id, after_cursor=cursor, limit=1000)
            entries.extend(page.entries)
            if len(page.entries) < 1000:
                return entries
            cursor = page.entries[-1].cursor

    async def crash(self) -> None:
        status = await (await self.control()).GetStatus(session_pb2.GetStatusRequest())
        await self.process.crash([process.pid for process in status.processes if process.HasField("pid")])
        await self.process.close()

    async def host_pid(self, pid: int) -> int:
        return await self.process.host_pid(pid)


async def kill_harness(runner: RunningRunner, status: session_pb2.EnvironmentRuntimeStatus, session_id: str) -> None:
    """SIGKILL the harness process `status` assigns `session_id` to, and wait until it is gone."""
    process = next(row for row in status.processes if session_id in row.assigned_session_ids)
    assert process.HasField("pid")
    pid = await runner.host_pid(process.pid)
    os.kill(pid, signal.SIGKILL)
    await asyncio.wait_for(wait_for_exit(pid), timeout=10)
