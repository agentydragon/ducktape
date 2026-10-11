"""The runner ingress: a runner pushes each Session's journal here, one call per Session.

Contract: agentplane/docs/runner_control.md § Event ingress. Every acknowledgement follows a
commit, so the runner's resume point is always durable here; `Store.append` accepts exact replays
and serializes concurrent calls for one Session, so any replica serves any call.
"""

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import override
from uuid import UUID

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.history_service.storage.store import (
    Checkpoint,
    HistoryConflictError,
    Store,
    Writer,
    WriterMismatchError,
)
from agentplane.proto import event_log_pb2, runner_ingress_pb2, runner_ingress_pb2_grpc, runner_pb2
from agentplane.proto.history_service_pb2 import SessionFeedState
from agentplane.workload_auth.grpc_caller import resolve_caller
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Timing:
    # The ingress repeats its acknowledgement after this long without a frame.
    idle_ack_s: float = 10
    # And ends the call after this long without one.
    silence_limit_s: float = 30


@dataclass(frozen=True)
class Resources:
    principals: WorkloadPrincipalResolver
    history: Store
    timing: Timing


class RefusedError(Exception):
    def __init__(self, code: grpc.StatusCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _EndOfCall:
    pass


class CallEndedError(Exception):
    """The runner closed its side of the call."""


class _Frames:
    """Reads the call's frames on a task, so waiting for one with a timeout never cancels a read."""

    def __init__(self, frames: AsyncIterable[runner_ingress_pb2.PushFrame]) -> None:
        self._source = frames
        self._queue: asyncio.Queue[runner_ingress_pb2.PushFrame | _EndOfCall | BaseException] = asyncio.Queue()

    async def pump(self) -> None:
        try:
            async for frame in self._source:
                self._queue.put_nowait(frame)
            self._queue.put_nowait(_EndOfCall())
        except Exception as error:
            self._queue.put_nowait(error)

    def ready(self) -> bool:
        return not self._queue.empty()

    async def next(self, timeout_s: float) -> runner_ingress_pb2.PushFrame | None:
        """The next frame, or None after `timeout_s` without one."""
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout_s)
        except TimeoutError:
            return None
        if isinstance(item, _EndOfCall):
            raise CallEndedError
        if isinstance(item, BaseException):
            raise item
        return item


def _acknowledgement(checkpoint: Checkpoint) -> runner_ingress_pb2.Acknowledgement:
    return runner_ingress_pb2.Acknowledgement(cursor=checkpoint.cursor, sealed=checkpoint.sealed)


def _session_id(announce: runner_pb2.Attached) -> UUID:
    try:
        session_id = UUID(announce.session_id)
    except ValueError:
        raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "the Session ID is not a UUID") from None
    # The feed snapshot is stored under the canonical spelling, so only that one is accepted.
    if str(session_id) != announce.session_id:
        raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "the Session ID is not a canonical UUID")
    return session_id


class _Journal:
    """One call's view of a Session: what it committed and what the runner may send next."""

    def __init__(self, history: Store, session_id: UUID, announce: runner_pb2.Attached, checkpoint: Checkpoint) -> None:
        self.history = history
        self.session_id = session_id
        self.announce = announce
        self.committed = checkpoint.cursor
        # The cursor of the last entry this call carried; the first may start anywhere through
        # one past the committed cursor.
        self.sent: int | None = None
        self.batch: list[event_log_pb2.EventEntry] = []
        self.snapshot_recorded = False

    def take(self, entry: event_log_pb2.EventEntry) -> None:
        if self.sent is None:
            if not 1 <= entry.cursor <= self.committed + 1:
                raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "the first entry leaves a gap")
        elif entry.cursor != self.sent + 1:
            raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "entries are not consecutive")
        self.sent = entry.cursor
        self.batch.append(entry)

    async def commit(self) -> None:
        if self.batch:
            self.committed = await self.history.append(self.session_id, self.batch)
            self.batch.clear()
        # The announced snapshot is stored once history covers it.
        if not self.snapshot_recorded and self.committed >= self.announce.last_cursor:
            await self.history.record_feed_state(self.session_id, SessionFeedState(attached=self.announce))
            self.snapshot_recorded = True

    async def seal(self, sealed: runner_pb2.Sealed) -> None:
        if sealed.cursor != (self.sent if self.sent is not None else self.committed):
            raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "the seal does not follow the last entry")
        await self.commit()
        await self.history.record_feed_state(self.session_id, SessionFeedState(attached=self.announce, sealed=sealed))


class RunnerIngressService(runner_ingress_pb2_grpc.RunnerIngressServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @override
    async def Push(
        self, request_iterator: AsyncIterator[runner_ingress_pb2.PushFrame], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_ingress_pb2.Acknowledgement]:
        session_id: UUID | None = None
        frames = _Frames(request_iterator)
        pump = asyncio.create_task(frames.pump())
        try:
            writer = await self._authenticate(context)
            try:
                first = await frames.next(self.resources.timing.silence_limit_s)
            except CallEndedError:
                raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "the call ended unannounced") from None
            if first is None:
                raise RefusedError(grpc.StatusCode.UNAVAILABLE, "no announcement")
            if first.WhichOneof("frame") != "announce":
                raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "the first frame is not an announcement")
            session_id = _session_id(first.announce)
            checkpoint = await self.resources.history.bind_writer(session_id, writer)
            yield _acknowledgement(checkpoint)
            if checkpoint.sealed:
                return
            journal = _Journal(self.resources.history, session_id, first.announce, checkpoint)
            await journal.commit()
            async for acknowledgement in self._receive(frames, journal):
                yield acknowledgement
        except RefusedError as refused:
            await context.abort(refused.code, refused.detail)
        except WorkloadPrincipalRejectedError:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
        except WriterMismatchError:
            logger.exception("Session %s push refused: the Session belongs to another writer", session_id)
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "the Session belongs to another writer")
        except HistoryConflictError:
            logger.exception("Session %s push refused", session_id)
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "the journal differs from committed history")
        except ConnectionError, TimeoutError, SQLAlchemyError, k8s_client.ApiException:
            logger.warning("Session %s push failed", session_id, exc_info=True)
            await context.abort(grpc.StatusCode.UNAVAILABLE, "history or authentication unavailable")
        finally:
            pump.cancel()

    async def _receive(self, frames: _Frames, journal: _Journal) -> AsyncIterator[runner_ingress_pb2.Acknowledgement]:
        timing = self.resources.timing
        silent_s = 0.0
        while True:
            try:
                frame = await frames.next(timing.idle_ack_s)
            except CallEndedError:
                # The runner reconnects and resumes after the last acknowledgement; keep what arrived.
                await journal.commit()
                return
            if frame is None:
                silent_s += timing.idle_ack_s
                if silent_s >= timing.silence_limit_s:
                    raise RefusedError(grpc.StatusCode.UNAVAILABLE, "no frame within the silence limit")
                yield runner_ingress_pb2.Acknowledgement(cursor=journal.committed)
                continue
            silent_s = 0.0
            match frame.WhichOneof("frame"):
                case "heartbeat":
                    pass
                case "entry":
                    journal.take(frame.entry)
                    # Commit what has arrived, in one transaction, once nothing more is waiting.
                    if not frames.ready():
                        await journal.commit()
                        yield runner_ingress_pb2.Acknowledgement(cursor=journal.committed)
                case "sealed":
                    await journal.seal(frame.sealed)
                    yield runner_ingress_pb2.Acknowledgement(cursor=journal.committed, sealed=True)
                    return
                case _:
                    raise RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "unexpected frame")

    async def _authenticate(self, context: grpc.aio.ServicerContext) -> Writer:
        principal = await resolve_caller(self.resources.principals, context)
        return Writer(
            namespace=principal.namespace, name=principal.service_account_name, uid=principal.service_account_uid
        )


def add_ingress(resources: Resources, server: grpc.aio.Server) -> None:
    runner_ingress_pb2_grpc.add_RunnerIngressServicer_to_server(RunnerIngressService(resources), server)
