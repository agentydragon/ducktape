"""Generic asynchronous gRPC control service for provider process backends."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NoReturn, TypeVar, override

import grpc

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider import session_provider_pb2 as pb, session_provider_pb2_grpc as pb_grpc, validation
from platform_sessions.provider.instance_ownership import InstanceOwnership

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

    async def finish_interactive_sign_in(self, instance_id: str) -> pb.ProbeResult:
        """Verify and persist an owner-operated browser session when advertised in info."""
        raise NotImplementedError


class SessionProviderControlService(pb_grpc.SessionProviderControlServicer):
    """Validate control requests and translate a typed provider backend into gRPC responses.

    This service does not implement peer authentication or bind a listener. Register it only on a
    private server configured by its caller with the deployment's authenticated transport.
    """

    def __init__(self, provider: ProviderBackend, ownership: Mapping[str, InstanceOwnership]) -> None:
        self._provider_info = pb.ProviderInfo()
        self._provider_info.CopyFrom(provider.info)
        validation.provider_info(self._provider_info)
        self._provider = provider
        self._instance_ids = frozenset(instance.instance_id for instance in self._provider_info.instances)
        if set(ownership) != self._instance_ids:
            raise ValueError("ownership is required for every provider instance")
        self._ownership = dict(ownership)
        self._operations: set[asyncio.Task[Any]] = set()
        self._instance_locks = {instance_id: asyncio.Lock() for instance_id in self._instance_ids}
        self._stopping = False
        self._closed = False
        self._shutdown_lock = asyncio.Lock()
        self._accepted_formats = {
            instance.instance_id: frozenset(item.media_type for item in instance.accepted_session_import_formats)
            for instance in self._provider_info.instances
        }
        self._interactive_instances = frozenset(
            instance.instance_id
            for instance in self._provider_info.instances
            if instance.HasField("interactive_sign_in")
        )

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
    ) -> pb.ImportSessionResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        if not request.media_type or not request.candidate:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "session import fields are required")
        if len(request.candidate) > MAX_SESSION_BYTES:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "session import exceeds the size limit")

        return await self._run_operation(context, instance_id, lambda: self._import(request, instance_id))

    async def _run_operation(
        self, context: grpc.aio.ServicerContext, instance_id: str, action: Callable[[], Awaitable[_T]]
    ) -> _T:
        if self._stopping:
            await self._abort(context, grpc.StatusCode.UNAVAILABLE, "provider is stopping")

        async def serialized() -> _T:
            async with self._instance_locks[instance_id]:
                return await action()

        # Accepted work outlives its transport: cancellation must not interrupt credential
        # persistence or let a successor owner start while the operation is still running.
        task = asyncio.create_task(serialized())
        self._operations.add(task)
        task.add_done_callback(self._operation_finished)
        return await asyncio.shield(task)

    async def drain(self, *, drain_seconds: float = 5.0) -> bool:
        """Stop admitting operations and wait for accepted operations to finish.

        Instance ownership is retained even after a successful drain. The caller
        must stop the listener and provider owner before calling close().
        """
        async with self._shutdown_lock:
            if self._closed:
                return True
            self._stopping = True
            if self._operations:
                _, pending = await asyncio.wait(self._operations, timeout=drain_seconds)
                if pending:
                    return False
            return True

    async def close(self) -> None:
        """Release instance ownership after draining operations and stopping the owner."""
        async with self._shutdown_lock:
            if self._closed:
                return
            if not self._stopping or any(not task.done() for task in self._operations):
                raise RuntimeError("provider operations must be drained before releasing ownership")
            for owner in self._ownership.values():
                await asyncio.to_thread(owner.close)
            self._closed = True

    def _operation_finished(self, task: asyncio.Task[Any]) -> None:
        self._operations.discard(task)
        if not task.cancelled():
            task.exception()  # Consume failures if the caller disconnected.

    async def _import(self, request: pb.ImportSessionRequest, instance_id: str) -> pb.ImportSessionResponse:
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
                validation.import_result(outcome)
            except Exception as error:
                # Exception text can contain credentials. A failed call may already have
                # consumed them; neither the server nor its caller may retry automatically.
                logger.error(  # noqa: TRY400 - exception text may contain credentials
                    "provider import failed provider_id=%s instance_id=%s error_type=%s",
                    self._provider_info.provider_id,
                    instance_id,
                    type(error).__name__,
                )
                outcome = pb.ImportSessionResponse(
                    outcome_unknown=pb.OperationOutcomeUnknown(credential_rotation_may_have_occurred=True)
                )
        response = pb.ImportSessionResponse()
        response.CopyFrom(outcome)
        response.DiscardUnknownFields()
        response.instance.CopyFrom(request.instance)
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
    async def FinishInteractiveSignIn(
        self, request: pb.FinishInteractiveSignInRequest, context: grpc.aio.ServicerContext
    ) -> pb.FinishInteractiveSignInResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        if instance_id not in self._interactive_instances:
            await self._abort(context, grpc.StatusCode.FAILED_PRECONDITION, "interactive sign-in is unavailable")
        outcome = await self._call_provider(
            context,
            operation="finish_interactive_sign_in",
            instance_id=instance_id,
            action=lambda: self._provider.finish_interactive_sign_in(instance_id),
        )
        try:
            if not isinstance(outcome, pb.ProbeResult):
                raise validation.ProviderProtocolError("Invalid result type")
            result = pb.ProbeResult()
            result.CopyFrom(outcome)
            result.source = pb.PROBE_SOURCE_VERIFY
            if not result.HasField("completed_at"):
                result.completed_at.GetCurrentTime()
            validation.probe_result(result)
        except validation.ProviderProtocolError:
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")
        return pb.FinishInteractiveSignInResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id), result=result
        )

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
        async def call() -> _T:
            try:
                return await action()
            except Exception as error:
                # Provider exception text may contain credentials or private upstream data.
                logger.log(
                    logging.ERROR,
                    "provider control operation failed operation=%s provider_id=%s instance_id=%s error_type=%s",
                    operation,
                    self._provider_info.provider_id,
                    instance_id,
                    type(error).__name__,
                )
                await self._abort(context, grpc.StatusCode.INTERNAL, "provider operation failed")

        return await self._run_operation(context, instance_id, call)

    async def _abort(self, context: grpc.aio.ServicerContext, code: grpc.StatusCode, detail: str) -> NoReturn:
        await context.abort(code, detail)


def add_session_provider_control_service(
    server: grpc.aio.Server, provider: ProviderBackend, ownership: Mapping[str, InstanceOwnership]
) -> SessionProviderControlService:
    """Register the control service on a caller-configured, private authenticated server."""
    servicer = SessionProviderControlService(provider, ownership)
    pb_grpc.add_SessionProviderControlServicer_to_server(servicer, server)
    return servicer
