"""The gRPC surface: Attach streams over the sessions one runner process owns."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import PurePosixPath

import grpc

from x.agentplane.runner import protocol_pb2 as pb, protocol_pb2_grpc
from x.agentplane.runner.adapter import HarnessAdapter
from x.agentplane.runner.claude import ClaudeAdapter
from x.agentplane.runner.codex import CodexAdapter
from x.agentplane.runner.config import RunnerConfig
from x.agentplane.runner.session import Attachment, Session
from x.agentplane.runner.store import SessionRecord, SessionStore, validate_session_id

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
_BOOTSTRAP_KEY = re.compile(r"^[a-f0-9]{64}$")
_MAX_BOOTSTRAP_BYTES = 65_536
_MAX_BOOTSTRAP_OUTPUT = 16_384


class OpenError(Exception):
    """Open named a session the runner cannot serve; the stream ends with this message."""


class InitializationConflictError(Exception):
    """The sandbox already completed a different bootstrap initialization."""


def make_adapter(session: Session) -> HarnessAdapter:
    provider = pb.Provider.Value(session.record.provider)
    if provider == pb.PROVIDER_CLAUDE:
        if session.config.claude is None:
            raise RuntimeError("this runner is not configured for Claude sessions")
        return ClaudeAdapter(session, session.config.claude)
    if provider == pb.PROVIDER_CODEX:
        if session.config.codex is None:
            raise RuntimeError("this runner is not configured for Codex sessions")
        return CodexAdapter(session, session.config.codex)
    raise ValueError(f"unsupported {provider=}")


class Runner:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.store = SessionStore(config.state_dir / "sessions")
        self.sessions: dict[str, Session] = {}
        self._initialize_lock = asyncio.Lock()

    async def initialize(self, request: pb.InitializeRequest) -> pb.InitializeResult:
        """Run exactly one configured bootstrap script for this sandbox's persistent state."""
        if _BOOTSTRAP_KEY.fullmatch(request.key) is None:
            raise ValueError("initialize.key must be a lowercase SHA-256 digest")
        source = request.script.encode()
        if not source or len(source) > _MAX_BOOTSTRAP_BYTES:
            raise ValueError(f"initialize.script must contain 1..{_MAX_BOOTSTRAP_BYTES} UTF-8 bytes")
        marker_dir = self.config.state_dir / "initializations"
        marker = marker_dir / request.key
        script_digest = hashlib.sha256(source).hexdigest()
        async with self._initialize_lock:
            completed = sorted(path for path in marker_dir.glob("*") if path.is_file())
            if completed:
                if len(completed) != 1 or completed[0].name != request.key:
                    identities = ", ".join(path.name for path in completed)
                    raise InitializationConflictError(
                        f"sandbox already initialized with a different bootstrap ({identities}); refusing {request.key}"
                    )
                recorded_digest = marker.read_text().strip()
                if recorded_digest != script_digest:
                    raise InitializationConflictError(
                        "sandbox bootstrap identity matches, but its script differs from the completed initialization"
                    )
                return pb.InitializeResult(key=request.key, executed=False, exit_code=0)
            marker_dir.mkdir(parents=True, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-eu",
                cwd=self.config.state_dir,
                env={**os.environ, **self.config.environment},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate(source)
            exit_code = process.returncode
            assert exit_code is not None
            result = pb.InitializeResult(
                key=request.key,
                executed=True,
                exit_code=exit_code,
                stdout=stdout[-_MAX_BOOTSTRAP_OUTPUT:].decode(errors="replace"),
                stderr=stderr[-_MAX_BOOTSTRAP_OUTPUT:].decode(errors="replace"),
            )
            if exit_code == 0:
                marker.write_text(f"{script_digest}\n")
            return result

    def startup(self) -> None:
        """Load every stored session and record what the previous runner process took with it."""
        for session_id in self.store.session_ids():
            self._load(session_id).recover_after_restart()

    def _load(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(
                session_id,
                record=self.store.read(session_id),
                store=self.store,
                config=self.config,
                make_adapter=make_adapter,
            )
        return self.sessions[session_id]

    async def open(self, request: pb.Open) -> Session:
        try:
            session_id = validate_session_id(request.session_id)
        except ValueError as error:
            raise OpenError(str(error)) from error
        if self.store.exists(session_id):
            session = self._load(session_id)
            if request.HasField("spec") and request.spec != session.record.spec():
                raise OpenError(f"session {session_id} exists with a different spec")
        else:
            if not request.HasField("spec"):
                raise OpenError(f"session {session_id} does not exist and Open carries no spec")
            if request.spec.provider not in (pb.PROVIDER_CLAUDE, pb.PROVIDER_CODEX):
                raise OpenError("spec.provider must be CLAUDE or CODEX")
            if not request.spec.cwd or not request.spec.model:
                raise OpenError("spec.cwd and spec.model are required")
            if not PurePosixPath(request.spec.cwd).is_absolute():
                raise OpenError(f"spec.cwd must be an absolute path, not {request.spec.cwd!r}")
            record = SessionRecord.from_spec(request.spec)
            self.store.write(session_id, record)
            session = Session(
                session_id, record=record, store=self.store, config=self.config, make_adapter=make_adapter
            )
            self.sessions[session_id] = session
        await session.ensure_running()
        return session

    async def stop(self) -> None:
        await asyncio.gather(*(session.stop() for session in self.sessions.values()))

    def summaries(self) -> list[pb.SessionSummary]:
        return [
            pb.SessionSummary(
                session_id=session.session_id,
                spec=session.record.spec(),
                last_sequence=session.log.last_sequence,
                harness=pb.HARNESS_STATE_RUNNING if session.running else pb.HARNESS_STATE_STOPPED,
                active_turn_id=session.active_turn_id,
            )
            for session in sorted(self.sessions.values(), key=lambda session: session.session_id)
        ]


class RunnerService(protocol_pb2_grpc.RunnerServicer):
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    async def Initialize(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: pb.InitializeRequest, context: grpc.aio.ServicerContext
    ) -> pb.InitializeResult:
        try:
            return await self.runner.initialize(request)
        except InitializationConflictError as error:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
            raise AssertionError("context.abort always raises") from error
        except ValueError as error:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
            raise AssertionError("context.abort always raises") from error

    async def ListSessions(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: pb.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> pb.ListSessionsResponse:
        del request, context
        return pb.ListSessionsResponse(sessions=self.runner.summaries())

    async def Attach(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request_iterator: AsyncIterator[pb.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.ServerMessage]:
        del context
        first = await anext(request_iterator, None)
        if first is None or not first.HasField("open"):
            yield pb.ServerMessage(error="the first client message must be Open")
            return
        try:
            session = await self.runner.open(first.open)
        except OpenError as error:
            yield pb.ServerMessage(error=str(error))
            return
        except Exception as error:  # the stream must report a launch failure, not hang
            logger.exception("session %s: open failed", first.open.session_id)
            yield pb.ServerMessage(error=f"open failed: {error}")
            return
        if first.open.after_sequence > session.log.last_sequence:
            yield pb.ServerMessage(
                error=f"after_sequence {first.open.after_sequence} is beyond the session log, "
                f"whose last sequence is {session.log.last_sequence}"
            )
            return
        attachment = session.attach()
        yield pb.ServerMessage(
            attached=pb.Attached(
                session_id=session.session_id,
                spec=session.record.spec(),
                last_sequence=session.log.last_sequence,
                harness=pb.HARNESS_STATE_RUNNING if session.running else pb.HARNESS_STATE_STOPPED,
                active_turn_id=session.active_turn_id,
            )
        )
        closing = asyncio.Event()
        failure: list[str] = []
        consumer = asyncio.create_task(
            _consume(session, request_iterator, closing, failure), name=f"{session.session_id}-commands"
        )
        cursor = first.open.after_sequence
        try:
            while True:
                for event in session.log.since(cursor):
                    yield pb.ServerMessage(event=event)
                    cursor = event.sequence
                if attachment.superseded.is_set():
                    yield pb.ServerMessage(error="superseded by a newer attachment")
                    return
                if closing.is_set():
                    if failure:
                        yield pb.ServerMessage(error=failure[0])
                    return
                await _wake(session, attachment, closing, cursor)
        finally:
            if not consumer.done():
                consumer.cancel()


async def _wake(session: Session, attachment: Attachment, closing: asyncio.Event, cursor: int) -> None:
    """Return when there is a new event, the attachment is superseded, or the stream is closing."""
    waits = [
        asyncio.create_task(session.log.wait_beyond(cursor)),
        asyncio.create_task(attachment.superseded.wait()),
        asyncio.create_task(closing.wait()),
    ]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()


async def _consume(
    session: Session, requests: AsyncIterator[pb.ClientMessage], closing: asyncio.Event, failure: list[str]
) -> None:
    try:
        async for message in requests:
            match message.WhichOneof("command"):
                case "input":
                    await session.submit(message.input.input_id, message.input.text)
                case "interrupt":
                    await session.interrupt()
                case "shutdown":
                    await session.shutdown()
                    return
                case "detach":
                    return
                case other:
                    failure.append(f"unexpected client command after Open: {other}")
                    return
    except asyncio.CancelledError:
        raise
    except Exception as error:  # a command failure ends the stream with its reason, not a hang
        logger.exception("session %s: command failed", session.session_id)
        failure.append(f"command failed: {error}")
    finally:
        closing.set()


async def serve(config: RunnerConfig, *, address: str = "127.0.0.1:0") -> tuple[grpc.aio.Server, Runner, int]:
    """Start a runner and its server; the returned port is the bound one."""
    runner = Runner(config)
    runner.startup()
    server = grpc.aio.server()
    protocol_pb2_grpc.add_RunnerServicer_to_server(RunnerService(runner), server)
    port = server.add_insecure_port(address)
    await server.start()
    return server, runner, port
