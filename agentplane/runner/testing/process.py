"""Own a real Go runner subprocess and observe it through its public protocol."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Self
from uuid import uuid4

from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner import runner_service_pb2_grpc
from agentplane.runner.configuration import Endpoint
from agentplane.runner.testing import launches
from agentplane.runner.testing.config import RunnerTestConfig
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process_fixtures import wait_for_exit
from util.testing.undeclared_outputs import undeclared_outputs_dir


class RunningRunner:
    def __init__(
        self,
        config: RunnerTestConfig,
        gateway: Gateway,
        *,
        history: HistoryServiceClient,
        ingress_endpoint: Endpoint,
        control_endpoint: Endpoint | None = None,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.history = history
        self.control_endpoint = control_endpoint or gateway.endpoint
        self.ingress_endpoint = ingress_endpoint
        self.process: asyncio.subprocess.Process
        self.log_path = undeclared_outputs_dir() / f"go-runner-{uuid4().hex}.log"

    @property
    def state_dir(self) -> Path:
        return self.config.state_dir

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        connections = self.gateway.control.connections
        environment = dict(self.config.harness_environment)
        environment.update(
            {key: os.environ[key] for key in ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE") if key in os.environ}
        )
        environment["OTEL_SDK_DISABLED"] = "true"
        if self.config.claude is not None:
            environment["ANTHROPIC_AUTH_TOKEN"] = self.config.claude.token
        if self.config.codex is not None:
            environment["OPENAI_API_KEY"] = self.config.codex.token
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(
                *launches.command(self.config, control=self.control_endpoint, ingress=self.ingress_endpoint),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        if self.control_endpoint != self.gateway.endpoint:
            return  # The real Gateway's fixture owns its authenticated readiness probe.
        connected = asyncio.create_task(self.gateway.control.wait_ready(connections + 1))
        exited = asyncio.create_task(self.process.wait())
        try:
            done, _ = await asyncio.wait((connected, exited), return_when=asyncio.FIRST_COMPLETED)
            if exited in done:
                raise RuntimeError(f"Go runner exited with {exited.result()}; see {self.log_path}")
            await connected
        except BaseException:
            await self.stop()
            raise
        finally:
            connected.cancel()
            exited.cancel()
            await asyncio.gather(connected, exited, return_exceptions=True)

    async def stop(self) -> None:
        if self.process.returncode is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            async with asyncio.timeout(20):
                await self.process.wait()
        except TimeoutError:
            os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
            raise TimeoutError(f"Go runner did not stop; see {self.log_path}") from None
        if self.process.returncode:
            raise RuntimeError(f"Go runner exited with {self.process.returncode}; see {self.log_path}")

    async def restart(
        self, *, control_endpoint: Endpoint | None = None, ingress_endpoint: Endpoint | None = None
    ) -> None:
        await self.stop()
        if control_endpoint is not None:
            self.control_endpoint = control_endpoint
        if ingress_endpoint is not None:
            self.ingress_endpoint = ingress_endpoint
        await self.start()

    async def control(self) -> runner_service_pb2_grpc.RunnerAsyncStub:
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
        status = await (await self.control()).GetStatus(runner_pb2.GetStatusRequest())
        os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()
        for process in status.processes:
            if not process.HasField("pid"):
                continue
            await asyncio.wait_for(wait_for_exit(process.pid), timeout=10)


async def kill_harness(status: runner_pb2.RunnerStatus, session_id: str) -> None:
    """SIGKILL the harness process `status` assigns `session_id` to, and wait until it is gone."""
    process = next(row for row in status.processes if session_id in row.assigned_session_ids)
    assert process.HasField("pid")
    os.kill(process.pid, signal.SIGKILL)
    await asyncio.wait_for(wait_for_exit(process.pid), timeout=10)
