"""Generic asynchronous gRPC control service for provider process backends."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn, TypeVar, override

import grpc
from google.protobuf import empty_pb2

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider import session_provider_pb2 as pb, session_provider_pb2_grpc as pb_grpc, validation
from platform_sessions.provider.import_journal import ImportJournal, ImportRecord, ImportState, validate_operation_id

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class ProviderBackend(ABC):
    """Local provider operations used by the control-plane adapter.

    The backend owns provider-specific persistence, upstream calls, and any cookie or token
    rotation. It returns a protobuf result with exactly one outcome variant set. The service supplies the
    response instance and probe source, and supplies a completion time if omitted. If an upstream call may have
    rotated credentials but its result is ambiguous, return OperationOutcomeUnknown and persist
    that uncertainty in the local session snapshot. Do not raise an exception for that known case.

    An UpstreamUnavailable outcome is valid only when the provider can establish that no
    credential-changing effect is unresolved. The adapter never retries provider operations.
    """

    @property
    @abstractmethod
    def info(self) -> pb.ProviderInfo:
        """Process identity and metadata for all configured instances."""

    @abstractmethod
    async def get_session(self, instance_id: str) -> pb.SessionSnapshot:
        """Read durable local state without contacting the upstream."""

    @abstractmethod
    async def import_session(self, instance_id: str, *, media_type: str, candidate: bytes) -> pb.ImportSessionResponse:
        """Install and durably persist a candidate; never log or return candidate bytes."""

    @abstractmethod
    async def probe(self, instance_id: str, *, source: pb.ProbeSource) -> pb.ProbeResult:
        """Verify one session and persist any credential rotation before returning."""

    async def verify_candidate(
        self, instance_id: str, *, media_type: str, candidate: bytes
    ) -> pb.VerifyCandidateResponse:
        """Verify a candidate without installing it or updating installed-session probe state."""
        raise NotImplementedError("Provider advertised candidate verification without implementing it")


class SessionProviderControlService(pb_grpc.SessionProviderControlServicer):
    """Validate control requests and translate a typed provider backend into gRPC responses.

    This service does not implement peer authentication or bind a listener. Register it only on a
    private server configured by its caller with the deployment's authenticated transport.
    """

    def __init__(self, provider: ProviderBackend, journals: Mapping[str, ImportJournal]) -> None:
        self._provider_info = pb.ProviderInfo()
        self._provider_info.CopyFrom(provider.info)
        validation.provider_info(self._provider_info)
        self._provider = provider
        self._instance_ids = frozenset(instance.instance_id for instance in self._provider_info.instances)
        if set(journals) != self._instance_ids:
            raise ValueError("a journal is required for every provider instance")
        self._journals = dict(journals)
        self._imports: set[asyncio.Task[pb.ImportOperation]] = set()
        self._stopping = False
        self._closed = False
        self._shutdown_lock = asyncio.Lock()
        self._accepted_formats = {
            instance.instance_id: frozenset(item.media_type for item in instance.accepted_session_import_formats)
            for instance in self._provider_info.instances
        }
        self._supports_candidate_verification = {
            instance.instance_id: instance.supports_candidate_verification for instance in self._provider_info.instances
        }

    @override
    async def GetInfo(self, request: pb.GetInfoRequest, context: grpc.aio.ServicerContext) -> pb.GetInfoResponse:
        del request, context
        return pb.GetInfoResponse(provider=self._provider_info)

    @override
    async def GetSession(
        self, request: pb.GetSessionRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetSessionResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        snapshot = await self._call_provider(
            context,
            operation="get_session",
            instance_id=instance_id,
            action=lambda: self._provider.get_session(instance_id),
        )
        try:
            if not isinstance(snapshot, pb.SessionSnapshot):
                raise validation.ProviderProtocolError("Invalid snapshot type")
            validation.snapshot(snapshot)
        except validation.ProviderProtocolError:
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid session snapshot")
        return pb.GetSessionResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id),
            snapshot=snapshot,
        )

    @override
    async def ImportSession(
        self, request: pb.ImportSessionRequest, context: grpc.aio.ServicerContext
    ) -> pb.ImportOperation:
        instance_id = await self._resolve_instance(request.instance, context)
        try:
            validate_operation_id(request.operation_id)
        except ValueError:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid import operation ID")
        if not request.media_type or not request.candidate:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "session import fields are required")
        if len(request.candidate) > MAX_SESSION_BYTES:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "session import exceeds the size limit")

        if self._stopping:
            await self._abort(context, grpc.StatusCode.UNAVAILABLE, "provider is stopping")
        # This task owns reservation, dispatch, and result persistence. A cancelled
        # caller must not cancel it after an upstream operation has been dispatched.
        task = asyncio.create_task(self._import(request, instance_id))
        self._imports.add(task)
        task.add_done_callback(self._import_finished)
        try:
            return await asyncio.shield(task)
        except Exception:
            await self._abort(context, grpc.StatusCode.UNAVAILABLE, "import operation state unavailable")

    async def shutdown(self, *, drain_seconds: float = 5.0) -> bool:
        """Drain dispatched imports before releasing journals and their ownership locks.

        A false result retains those locks: the owner must terminate its process,
        not start a second owner while provider work may still be running.
        """
        async with self._shutdown_lock:
            if self._closed:
                return True
            self._stopping = True
            if self._imports:
                _, pending = await asyncio.wait(self._imports, timeout=drain_seconds)
                if pending:
                    return False
            for journal in self._journals.values():
                await asyncio.to_thread(journal.close)
            self._closed = True
            return True

    def _import_finished(self, task: asyncio.Task[pb.ImportOperation]) -> None:
        self._imports.discard(task)
        if not task.cancelled():
            task.exception()  # Consume failures if the caller disconnected.

    async def _import(self, request: pb.ImportSessionRequest, instance_id: str) -> pb.ImportOperation:
        journal = self._journals[instance_id]
        created, record = await asyncio.to_thread(journal.reserve, request.operation_id)
        if not created:
            return self._import_operation(request.instance, request.operation_id, record)

        if request.media_type not in self._accepted_formats[instance_id]:
            outcome = pb.ImportSessionResponse(
                rejected=pb.CandidateRejected(reason=pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT)
            )
        else:
            try:
                outcome = await self._provider.import_session(
                    instance_id, media_type=request.media_type, candidate=bytes(request.candidate)
                )
                if not isinstance(outcome, pb.ImportSessionResponse):
                    raise validation.ProviderProtocolError("invalid import result type")
                validation.candidate_result(outcome)
            except Exception as error:
                # Never serialize provider exception text or assume the call was not dispatched.
                logger.error(  # noqa: TRY400 - exception text may contain credentials
                    "provider import failed provider_id=%s instance_id=%s error_type=%s",
                    self._provider_info.provider_id,
                    instance_id,
                    type(error).__name__,
                )
                outcome = pb.ImportSessionResponse(
                    outcome_unknown=pb.OperationOutcomeUnknown(credential_rotation_may_have_occurred=True)
                )
        try:
            await asyncio.to_thread(journal.complete, request.operation_id, outcome)
        except Exception:
            # If result persistence fails, preserve ambiguity rather than presenting
            # the completed in-memory result as durable.
            try:
                await asyncio.to_thread(journal.mark_unknown, request.operation_id)
            except Exception:
                logger.error("could not record unknown import outcome instance_id=%s", instance_id)  # noqa: TRY400 - storage errors may contain sensitive paths
            raise
        completed_record = await asyncio.to_thread(journal.get, request.operation_id)
        assert completed_record is not None
        return self._import_operation(request.instance, request.operation_id, completed_record)

    @override
    async def GetImportOperation(
        self, request: pb.GetImportOperationRequest, context: grpc.aio.ServicerContext
    ) -> pb.ImportOperation:
        instance_id = await self._resolve_instance(request.instance, context)
        try:
            validate_operation_id(request.operation_id)
        except ValueError:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid import operation ID")
        try:
            record = await asyncio.to_thread(self._journals[instance_id].get, request.operation_id)
        except Exception:
            await self._abort(context, grpc.StatusCode.UNAVAILABLE, "import operation state unavailable")
        if record is None:
            await self._abort(context, grpc.StatusCode.NOT_FOUND, "import operation not found")
        return self._import_operation(request.instance, request.operation_id, record)

    @staticmethod
    def _import_operation(ref: pb.InstanceRef, operation_id: str, record: ImportRecord) -> pb.ImportOperation:
        response = pb.ImportOperation(instance=ref, operation_id=operation_id)
        if record.state is ImportState.PENDING:
            response.pending.CopyFrom(empty_pb2.Empty())
        elif record.state is ImportState.UNKNOWN:
            response.unknown.CopyFrom(empty_pb2.Empty())
        else:
            if record.result is None:
                raise validation.ProviderProtocolError("Completed import operation has no result")
            response.completed.CopyFrom(record.result)
            response.completed.instance.CopyFrom(ref)
        return response

    @override
    async def Probe(self, request: pb.ProbeRequest, context: grpc.aio.ServicerContext) -> pb.ProbeResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        if request.source not in (pb.PROBE_SOURCE_SCHEDULED, pb.PROBE_SOURCE_MANUAL):
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "probe source is required")

        outcome = await self._call_provider(
            context,
            operation="probe",
            instance_id=instance_id,
            action=lambda: self._provider.probe(instance_id, source=request.source),
        )
        try:
            if not isinstance(outcome, pb.ProbeResult):
                raise validation.ProviderProtocolError("Invalid result type")
            result = pb.ProbeResult()
            result.CopyFrom(outcome)
            result.source = request.source
            if not result.HasField("completed_at"):
                result.completed_at.GetCurrentTime()
            validation.probe_result(result)
        except validation.ProviderProtocolError:
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")
        return pb.ProbeResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id), result=result
        )

    @override
    async def VerifyCandidate(
        self, request: pb.VerifyCandidateRequest, context: grpc.aio.ServicerContext
    ) -> pb.VerifyCandidateResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        if not request.media_type or not request.candidate:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "candidate verification fields are required")
        if len(request.candidate) > MAX_SESSION_BYTES:
            await self._abort(
                context, grpc.StatusCode.INVALID_ARGUMENT, "candidate verification exceeds the size limit"
            )
        response = pb.VerifyCandidateResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id)
        )
        if not self._supports_candidate_verification[instance_id]:
            await self._abort(context, grpc.StatusCode.UNIMPLEMENTED, "Candidate verification is not supported")
        if request.media_type not in self._accepted_formats[instance_id]:
            response.rejected.reason = pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT
            return response

        outcome = await self._call_provider(
            context,
            operation="verify_candidate",
            instance_id=instance_id,
            action=lambda: self._provider.verify_candidate(
                instance_id, media_type=request.media_type, candidate=bytes(request.candidate)
            ),
        )
        try:
            if not isinstance(outcome, pb.VerifyCandidateResponse):
                raise validation.ProviderProtocolError("Invalid result type")
            validation.candidate_result(outcome)
        except validation.ProviderProtocolError:
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")
        response.CopyFrom(outcome)
        response.instance.CopyFrom(request.instance)
        return response

    async def _resolve_instance(self, reference: pb.InstanceRef, context: grpc.aio.ServicerContext) -> str:
        if not reference.provider_id or not reference.instance_id:
            await self._abort(
                context, grpc.StatusCode.INVALID_ARGUMENT, "provider and instance identifiers are required"
            )
        if reference.provider_id != self._provider_info.provider_id or reference.instance_id not in self._instance_ids:
            await self._abort(context, grpc.StatusCode.NOT_FOUND, "provider instance was not found")
        return reference.instance_id

    async def _call_provider(
        self,
        context: grpc.aio.ServicerContext,
        *,
        operation: str,
        instance_id: str,
        action: Callable[[], Awaitable[_T]],
    ) -> _T:
        try:
            return await action()
        except Exception as error:
            # Exception text can contain request data or upstream credentials. Keep the log and
            # gRPC status useful without serializing the exception, request, or candidate bytes.
            logger.log(
                logging.ERROR,
                "provider control operation failed operation=%s provider_id=%s instance_id=%s error_type=%s",
                operation,
                self._provider_info.provider_id,
                instance_id,
                type(error).__name__,
            )
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider operation failed")

    async def _abort(self, context: grpc.aio.ServicerContext, code: grpc.StatusCode, detail: str) -> NoReturn:
        await context.abort(code, detail)


def add_session_provider_control_service(
    server: grpc.aio.Server, provider: ProviderBackend, journals: Mapping[str, ImportJournal]
) -> SessionProviderControlService:
    """Register the control service on a caller-configured, private authenticated server."""
    servicer = SessionProviderControlService(provider, journals)
    pb_grpc.add_SessionProviderControlServicer_to_server(servicer, server)
    return servicer
