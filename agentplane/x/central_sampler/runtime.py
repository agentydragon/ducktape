"""Retained per-Session JS hosts and bounded, fenced command drivers."""

import asyncio
import json
import logging
import os
import shlex
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse

from agentplane.grpc.calls import dial
from agentplane.proto import command_pb2, runner_pb2
from agentplane.runner_gateway.executor_client import ExecutorSocket, connect
from agentplane.x.central_sampler import sampler_pb2
from agentplane.x.central_sampler.executor import Executor
from agentplane.x.central_sampler.host_limits import host_limits
from agentplane.x.central_sampler.settings import Settings
from agentplane.x.central_sampler.store import Store
from agentplane.x.postgres_codex.code_mode import CodeMode, code_mode
from agentplane.x.postgres_codex.compaction import compact
from agentplane.x.postgres_codex.control import controlled_advance
from agentplane.x.postgres_codex.sampler import pending_call
from agentplane.x.postgres_codex.store import KnownFailureError, LostOwnershipError, Owner
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.changed = asyncio.Event()
        self.slots = asyncio.Semaphore(settings.max_live_sessions)
        self.stopping = False

    @asynccontextmanager
    async def running(self) -> AsyncIterator[Runtime]:
        async with asyncio.TaskGroup() as group:
            recovery = group.create_task(self.recover())
            try:
                yield self
            finally:
                self.stopping = True
                recovery.cancel()
                for task in self.tasks.values():
                    task.cancel()
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def wake(self, session_id: str) -> None:
        if not self.stopping and session_id not in self.tasks and len(self.tasks) < self.settings.max_live_sessions:
            task = asyncio.create_task(self.drive(session_id))
            self.tasks[session_id] = task
            task.add_done_callback(lambda completed: self.finished(session_id, completed))
        self.changed.set()

    def finished(self, session_id: str, task: asyncio.Task[None]) -> None:
        self.tasks.pop(session_id)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Session driver failed for %s", session_id, exc_info=task.exception())
        self.changed.set()

    async def recover(self) -> None:
        while True:
            self.changed.clear()
            capacity = self.settings.max_live_sessions - len(self.tasks)
            if capacity:
                for session_id in await self.store.recoverable_sessions(exclude=set(self.tasks), limit=capacity):
                    self.wake(session_id)
            # Also discover commands admitted through another replica or just as a driver exits.
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=1)

    async def heartbeat(self, owner: Owner) -> None:
        while True:
            await self.store.renew(owner, lease=timedelta(seconds=self.settings.lease_seconds))
            await asyncio.sleep(self.settings.lease_seconds / 3)

    async def supervise(self, runtime: CodeMode) -> None:
        try:
            while True:
                if runtime.process.returncode is not None:
                    raise ChildProcessError("code-mode host exited")
                statm = await asyncio.to_thread(Path(f"/proc/{runtime.process.pid}/statm").read_text)
                rss = int(statm.split()[1]) * os.sysconf("SC_PAGE_SIZE")
                if rss > self.settings.host_rss_bytes:
                    raise MemoryError("code-mode host exceeded its configured RSS limit")
                await asyncio.sleep(0.1)
        except (ChildProcessError, FileNotFoundError, MemoryError) as error:
            runtime.fail(error)
            if runtime.process.returncode is None:
                runtime.process.kill()

    @asynccontextmanager
    async def executor(self, session_id: str, owner: Owner, http: ClientSession) -> AsyncIterator[Executor]:
        session = await self.store.get_session(session_id)
        request = sampler_pb2.OpenSessionRequest.FromString(session.configuration)
        async with AsyncExitStack() as stack:
            socket: ClientWebSocketResponse | ExecutorSocket
            if self.settings.direct_executor_url is not None:
                socket = await stack.enter_async_context(http.ws_connect(self.settings.direct_executor_url))
            else:
                assert self.settings.gateway_target is not None
                assert self.settings.gateway_token_file is not None
                channel = await stack.enter_async_context(
                    dial(self.settings.gateway_address(request.executor.owner.namespace, request.executor.sandbox))
                )
                socket = await stack.enter_async_context(
                    connect(
                        channel,
                        self.settings.gateway_token_file,
                        request.executor,
                        request.expected_incarnation_uid,
                        session_id,
                    )
                )
            executor = Executor(
                socket,
                cwd=Path(request.spec.cwd),
                codex_path=self.settings.executor_codex,
                environment={
                    "AGENTPLANE_DESTINATION": json.dumps(
                        {
                            "destination_ref": {
                                "namespace": request.executor.owner.namespace,
                                "name": request.executor.sandbox,
                                "uid": request.executor.sandbox_uid,
                            },
                            "session_id": session_id,
                        }
                    )
                },
            )
            executor.bind(self.store, owner)
            await executor.initialize()
            if not session.workspace_prepared:
                preparation = await executor.invoke(
                    "exec_command", {"cmd": "mkdir -p -- " + shlex.quote(request.spec.cwd), "workdir": "/"}
                )
                if preparation.get("exit_code") != 0:
                    raise RuntimeError("executor workspace preparation did not complete")
                await self.store.prepared_workspace(owner, session_id)
            yield executor

    @asynccontextmanager
    async def host(
        self, session_id: str, owner: Owner, http: ClientSession
    ) -> AsyncIterator[tuple[CodeMode, Executor]]:
        session = await self.store.get_session(session_id)
        async with self.slots, AsyncExitStack() as stack:
            executor = await stack.enter_async_context(self.executor(session_id, owner, http))
            prefix: tuple[str, ...] = ()
            if self.settings.host_cgroup_root is not None:
                group = stack.enter_context(
                    host_limits(
                        self.settings.host_cgroup_root,
                        memory_bytes=self.settings.host_rss_bytes,
                        pids=self.settings.host_pids,
                        cpu_quota_us=self.settings.host_cpu_quota_us,
                    )
                )
                prefix = (
                    str(get_required_path("_main/agentplane/x/central_sampler/host_launcher_bin")),
                    "--cgroup",
                    str(group),
                    "--",
                )
            runtime = await stack.enter_async_context(
                code_mode(
                    str(self.settings.code_mode_host),
                    self.settings.logs / f"{session.source_id}.log",
                    self.store,
                    owner,
                    executor,
                    command_prefix=prefix,
                )
            )
            monitor = asyncio.create_task(self.supervise(runtime))
            try:
                yield runtime, executor
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    async def setup(self, session_id: str, owner: Owner, http: ClientSession) -> bool:
        session = await self.store.get_session(session_id)
        if session.setup_state != runner_pb2.SETUP_STATE_RUNNING:
            return session.setup_state in {runner_pb2.SETUP_STATE_NOT_REQUIRED, runner_pb2.SETUP_STATE_SUCCEEDED}
        if not await self.store.start_setup(owner, session_id):
            return False
        request = sampler_pb2.OpenSessionRequest.FromString(session.configuration)

        async def output(stream: str, data: bytes) -> None:
            await self.store.setup_output(owner, session_id, stream, data)

        try:
            async with (
                asyncio.timeout(self.settings.turn_timeout_seconds),
                self.executor(session_id, owner, http) as executor,
            ):
                exit_code = await executor.setup(request.setup_script, output)
            await self.store.finish_setup(owner, session_id, exit_code)
            return exit_code == 0
        except asyncio.CancelledError:
            # A new owner observes setup_started and records interruption without replay.
            raise
        except Exception:
            logger.exception("Central sampler setup stopped for session %s", session_id)
            await self.store.finish_setup(owner, session_id, None)
            return False

    async def wait_interrupt(self, owner: Owner) -> None:
        while True:
            self.changed.clear()
            if await self.store.pending_interrupts(owner):
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=0.05)

    async def connect_host(
        self, stack: AsyncExitStack, session_id: str, owner: Owner, http: ClientSession
    ) -> tuple[CodeMode, Executor] | None:
        connection = asyncio.create_task(stack.enter_async_context(self.host(session_id, owner, http)))
        interruption = asyncio.create_task(self.wait_interrupt(owner))
        try:
            done, _ = await asyncio.wait({connection, interruption}, return_when=asyncio.FIRST_COMPLETED)
            if connection in done:
                return await connection
            return None
        finally:
            for task in (connection, interruption):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connection, interruption, return_exceptions=True)

    async def execute(self, session_id: str, owner: Owner) -> None:
        active_command: str | None = None
        try:
            async with (
                ClientSession(timeout=ClientTimeout(total=self.settings.turn_timeout_seconds)) as http,
                AsyncExitStack() as hosts,
            ):
                if not await self.setup(session_id, owner, http):
                    return
                live: tuple[CodeMode, Executor] | None = None
                while True:
                    self.changed.clear()
                    command = await self.store.next_command(owner, session_id)
                    if command is None:
                        await self.store.check_live_cells(owner, runtime_available=live is not None)
                        state = await self.store.execution_state(owner)
                        running = any(p["status"] != "exited_unread" for p in state["processes"])
                        if not state["cells"] and not running:
                            return
                        if live is None:
                            live = await self.connect_host(hosts, session_id, owner, http)
                        if live is not None and await live[1].reconcile_processes(
                            rpc_timeout_seconds=self.settings.rpc_timeout_seconds
                        ):
                            continue
                        with suppress(TimeoutError):
                            await asyncio.wait_for(self.changed.wait(), timeout=1)
                        continue
                    if command.outcome != sampler_pb2.COMMAND_OUTCOME_ACCEPTED:
                        continue
                    active_command = command.id
                    await self.store.check_recovery(owner)
                    session = await self.store.get_session(session_id)
                    config = sampler_pb2.OpenSessionRequest.FromString(session.configuration)
                    provider = self.settings.providers[config.spec.central_sampler.provider]
                    payload = command_pb2.Command.FromString(command.payload)
                    async with asyncio.timeout(self.settings.turn_timeout_seconds):
                        if payload.HasField("compact"):
                            await compact(
                                self.store,
                                owner,
                                http,
                                endpoint=provider.endpoint,
                                credential=provider.credential.get_secret_value(),
                                model=session.model,
                            )
                            result = "compact"
                        else:
                            while True:
                                row = await self.store.read(owner)
                                if (
                                    live is None
                                    and row.pending is None
                                    and pending_call(row.items) is not None
                                    and not await self.store.pending_interrupts(owner)
                                ):
                                    live = await self.connect_host(hosts, session_id, owner, http)
                                result = await controlled_advance(
                                    self.store,
                                    owner,
                                    live[0] if live else None,
                                    live[1] if live else None,
                                    http,
                                    endpoint=provider.endpoint,
                                    credential=provider.credential.get_secret_value(),
                                    model=session.model,
                                    reasoning_effort=session.effort or None,
                                    auto_compact_tokens=self.settings.auto_compact_tokens,
                                )
                                if result in {"done", "interrupted", "effects_unknown"}:
                                    break
                    await self.store.finish_command(owner, session_id, command.id, result)
                    active_command = None
                    if result == "effects_unknown":
                        return
                    if result == "interrupted":
                        await hosts.aclose()
                        live = None
        except asyncio.CancelledError:
            # Pending intent and live cell records survive shutdown; no synthetic success.
            raise
        except KnownFailureError as error:
            logger.warning("Central sampler rejected execution for session %s: %s", session_id, error)
            await self.store.fail_execution(owner, session_id, active_command, failure=error)
        except Exception:
            logger.exception("Central sampler execution stopped for session %s", session_id)
            await self.store.fail_execution(owner, session_id, active_command)

    async def drive(self, session_id: str) -> None:
        session = await self.store.get_session(session_id)
        try:
            owner = await self.store.claim(
                session.conversation_id, lease=timedelta(seconds=self.settings.lease_seconds)
            )
        except LostOwnershipError:
            return
        try:
            async with asyncio.TaskGroup() as tasks:
                renewal = tasks.create_task(self.heartbeat(owner))
                try:
                    await self.execute(session_id, owner)
                finally:
                    renewal.cancel()
        finally:
            await self.store.release(owner)


# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
