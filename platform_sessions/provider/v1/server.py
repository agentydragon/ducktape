"""Generic asynchronous gRPC control service for provider process backends."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import NoReturn, TypeVar, override

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as pb_grpc
from platform_sessions.routes import Route

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

type ImportSessionOutcome = (
    pb.SessionInstalled
    | pb.CandidateRejected
    | pb.ReauthenticationRequired
    | pb.ChallengeRequired
    | pb.UpstreamUnavailable
    | pb.OperationOutcomeUnknown
)
type ProbeOutcome = (
    pb.ProbeVerified
    | pb.ReauthenticationRequired
    | pb.ChallengeRequired
    | pb.UpstreamUnavailable
    | pb.OperationOutcomeUnknown
    | pb.ProbeNoSession
    | pb.ProbeFailure
)
type CandidateVerificationOutcome = (
    pb.CandidateVerified
    | pb.CandidateRejected
    | pb.ReauthenticationRequired
    | pb.ChallengeRequired
    | pb.UpstreamUnavailable
    | pb.OperationOutcomeUnknown
)


class ProbeSource(Enum):
    """Explicit source of a provider probe request."""

    SCHEDULED = pb.PROBE_SOURCE_SCHEDULED
    MANUAL = pb.PROBE_SOURCE_MANUAL


class ProviderBackend(ABC):
    """Local provider operations used by the control-plane adapter.

    The backend owns provider-specific persistence, upstream calls, and any cookie or token
    rotation. It returns a typed outcome for completed operations. If an upstream call may have
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
    async def import_session(
        self, instance_id: str, *, media_type: str, format_version: str, candidate: bytes
    ) -> ImportSessionOutcome:
        """Install and durably persist a candidate; never log or return candidate bytes."""

    @abstractmethod
    async def probe(self, instance_id: str, *, source: ProbeSource) -> ProbeOutcome:
        """Verify one session and persist any credential rotation before returning."""

    async def verify_candidate(
        self, instance_id: str, *, media_type: str, format_version: str, candidate: bytes
    ) -> CandidateVerificationOutcome | pb.CandidateVerificationUnsupported:
        """Verify a candidate without installing it or updating installed-session probe state."""
        del instance_id, media_type, format_version, candidate
        return pb.CandidateVerificationUnsupported()


class SessionProviderControlService(pb_grpc.SessionProviderControlServicer):
    """Validate control requests and translate a typed provider backend into gRPC responses.

    This service does not implement peer authentication or bind a listener. Register it only on a
    private server configured by its caller with the deployment's authenticated transport.
    """

    def __init__(self, provider: ProviderBackend) -> None:
        self._provider_info = pb.ProviderInfo()
        self._provider_info.CopyFrom(provider.info)
        self._validate_provider_info(self._provider_info)
        self._provider = provider
        self._instance_ids = frozenset(instance.instance_id for instance in self._provider_info.instances)
        self._accepted_formats = {
            instance.instance_id: frozenset(
                (item.media_type, item.version) for item in instance.accepted_session_import_formats
            )
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
        if not isinstance(snapshot, pb.SessionSnapshot) or not self._valid_snapshot(snapshot):
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
        if not request.media_type or not request.format_version or not request.candidate:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "session import fields are required")
        if len(request.candidate) > MAX_SESSION_BYTES:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "session import exceeds the size limit")
        if (request.media_type, request.format_version) not in self._accepted_formats[instance_id]:
            response = pb.ImportSessionResponse(
                instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id)
            )
            response.rejected.reason = pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT
            return response

        outcome = await self._call_provider(
            context,
            operation="import_session",
            instance_id=instance_id,
            action=lambda: self._provider.import_session(
                instance_id,
                media_type=request.media_type,
                format_version=request.format_version,
                candidate=bytes(request.candidate),
            ),
        )
        response = pb.ImportSessionResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id)
        )
        await self._set_import_outcome(response, outcome, context)
        return response

    @override
    async def Probe(self, request: pb.ProbeRequest, context: grpc.aio.ServicerContext) -> pb.ProbeResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        if request.source not in (pb.PROBE_SOURCE_SCHEDULED, pb.PROBE_SOURCE_MANUAL):
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "probe source is required")

        source = ProbeSource(request.source)
        outcome = await self._call_provider(
            context,
            operation="probe",
            instance_id=instance_id,
            action=lambda: self._provider.probe(instance_id, source=source),
        )
        result = pb.ProbeResult(source=request.source)
        result.completed_at.GetCurrentTime()
        await self._set_probe_outcome(result, outcome, context)
        return pb.ProbeResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id), result=result
        )

    @override
    async def VerifyCandidate(
        self, request: pb.VerifyCandidateRequest, context: grpc.aio.ServicerContext
    ) -> pb.VerifyCandidateResponse:
        instance_id = await self._resolve_instance(request.instance, context)
        if not request.media_type or not request.format_version or not request.candidate:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, "candidate verification fields are required")
        if len(request.candidate) > MAX_SESSION_BYTES:
            await self._abort(
                context, grpc.StatusCode.INVALID_ARGUMENT, "candidate verification exceeds the size limit"
            )
        response = pb.VerifyCandidateResponse(
            instance=pb.InstanceRef(provider_id=self._provider_info.provider_id, instance_id=instance_id)
        )
        if not self._supports_candidate_verification[instance_id]:
            response.unsupported.SetInParent()
            return response
        if (request.media_type, request.format_version) not in self._accepted_formats[instance_id]:
            response.rejected.reason = pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT
            return response

        outcome = await self._call_provider(
            context,
            operation="verify_candidate",
            instance_id=instance_id,
            action=lambda: self._provider.verify_candidate(
                instance_id,
                media_type=request.media_type,
                format_version=request.format_version,
                candidate=bytes(request.candidate),
            ),
        )
        await self._set_candidate_verification_outcome(response, outcome, context)
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

    @staticmethod
    def _validate_provider_info(provider: pb.ProviderInfo) -> None:
        if not provider.provider_id or not provider.display_name or not provider.version:
            raise ValueError("provider identity, display name, and version are required")
        instance_ids: set[str] = set()
        for instance in provider.instances:
            if not instance.instance_id or instance.instance_id in instance_ids:
                raise ValueError("provider instance identifiers must be present and unique")
            instance_ids.add(instance.instance_id)
            if instance.HasField("openapi_document_path"):
                SessionProviderControlService._validate_origin_path(instance.openapi_document_path)
            if instance.supports_candidate_verification and not instance.accepted_session_import_formats:
                raise ValueError("candidate verification requires a declared session import format")
            accepted_formats: set[tuple[str, str]] = set()
            for session_format in instance.accepted_session_import_formats:
                item = (session_format.media_type, session_format.version)
                if not all(item) or item in accepted_formats:
                    raise ValueError("session import formats must be complete and unique")
                accepted_formats.add(item)
            operation_ids: set[str] = set()
            for route in instance.routes:
                if not route.operation_id or route.operation_id in operation_ids:
                    raise ValueError("route operation identifiers must be present and unique per instance")
                operation_ids.add(route.operation_id)
                try:
                    Route(route.method, route.path_template)
                except ValueError as error:
                    raise ValueError("provider route descriptor has an invalid method or path") from error
                if route.access_class not in (
                    pb.ROUTE_ACCESS_CLASS_READ_ONLY,
                    pb.ROUTE_ACCESS_CLASS_STATE_CHANGING,
                    pb.ROUTE_ACCESS_CLASS_MIXED_OR_UNKNOWN,
                ):
                    raise ValueError("route access class must be explicit")

    @staticmethod
    def _validate_origin_path(path: str) -> None:
        try:
            Route("GET", path)
        except ValueError as error:
            raise ValueError("provider HTTP path must be a valid origin-relative path") from error

    @staticmethod
    def _valid_snapshot(snapshot: pb.SessionSnapshot) -> bool:
        if snapshot.state not in (
            pb.SESSION_STATE_NO_SESSION,
            pb.SESSION_STATE_UNVERIFIED,
            pb.SESSION_STATE_VERIFIED,
            pb.SESSION_STATE_REAUTHENTICATION_REQUIRED,
            pb.SESSION_STATE_CHALLENGE_REQUIRED,
            pb.SESSION_STATE_OUTCOME_UNKNOWN,
        ):
            return False
        if not snapshot.HasField("state_changed_at"):
            return False
        if not SessionProviderControlService._valid_timestamp(snapshot.state_changed_at):
            return False
        if snapshot.HasField("last_verified_at") and not SessionProviderControlService._valid_timestamp(
            snapshot.last_verified_at
        ):
            return False
        if snapshot.HasField("last_probe"):
            result = snapshot.last_probe
            if not SessionProviderControlService._valid_probe_result(result):
                return False
        return True

    @staticmethod
    def _valid_probe_result(result: pb.ProbeResult) -> bool:
        if (
            result.source not in (pb.PROBE_SOURCE_SCHEDULED, pb.PROBE_SOURCE_MANUAL)
            or not result.HasField("completed_at")
            or not SessionProviderControlService._valid_timestamp(result.completed_at)
        ):
            return False
        result_kind = result.WhichOneof("result")
        if result_kind is None:
            return False
        return result_kind != "failure" or result.failure.reason in (
            pb.PROBE_FAILURE_REASON_INVALID_RESPONSE,
            pb.PROBE_FAILURE_REASON_UPSTREAM_HTTP_ERROR,
            pb.PROBE_FAILURE_REASON_STORAGE_ERROR,
            pb.PROBE_FAILURE_REASON_SUPERSEDED,
            pb.PROBE_FAILURE_REASON_PROVIDER_INTERNAL,
        )

    @staticmethod
    def _valid_timestamp(timestamp: Timestamp) -> bool:
        try:
            timestamp.ToDatetime()
        except OverflowError, ValueError:
            return False
        return True

    async def _set_import_outcome(
        self, response: pb.ImportSessionResponse, outcome: ImportSessionOutcome, context: grpc.aio.ServicerContext
    ) -> None:
        if isinstance(outcome, pb.SessionInstalled):
            response.installed.CopyFrom(outcome)
        elif isinstance(outcome, pb.CandidateRejected):
            if outcome.reason not in (
                pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT,
                pb.CANDIDATE_REJECTION_REASON_MALFORMED,
                pb.CANDIDATE_REJECTION_REASON_NOT_ACCEPTED,
            ):
                await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")
            response.rejected.CopyFrom(outcome)
        elif isinstance(outcome, pb.ReauthenticationRequired):
            response.reauthentication_required.CopyFrom(outcome)
        elif isinstance(outcome, pb.ChallengeRequired):
            response.challenge_required.CopyFrom(outcome)
        elif isinstance(outcome, pb.UpstreamUnavailable):
            response.upstream_unavailable.CopyFrom(outcome)
        elif isinstance(outcome, pb.OperationOutcomeUnknown):
            response.outcome_unknown.CopyFrom(outcome)
        else:
            logger.error("provider control operation returned an invalid result operation=import_session")
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")

    async def _set_probe_outcome(
        self, result: pb.ProbeResult, outcome: ProbeOutcome, context: grpc.aio.ServicerContext
    ) -> None:
        if isinstance(outcome, pb.ProbeVerified):
            result.verified.CopyFrom(outcome)
        elif isinstance(outcome, pb.ReauthenticationRequired):
            result.reauthentication_required.CopyFrom(outcome)
        elif isinstance(outcome, pb.ChallengeRequired):
            result.challenge_required.CopyFrom(outcome)
        elif isinstance(outcome, pb.UpstreamUnavailable):
            result.upstream_unavailable.CopyFrom(outcome)
        elif isinstance(outcome, pb.OperationOutcomeUnknown):
            result.outcome_unknown.CopyFrom(outcome)
        elif isinstance(outcome, pb.ProbeNoSession):
            result.no_session.CopyFrom(outcome)
        elif isinstance(outcome, pb.ProbeFailure):
            if outcome.reason not in (
                pb.PROBE_FAILURE_REASON_INVALID_RESPONSE,
                pb.PROBE_FAILURE_REASON_UPSTREAM_HTTP_ERROR,
                pb.PROBE_FAILURE_REASON_STORAGE_ERROR,
                pb.PROBE_FAILURE_REASON_SUPERSEDED,
                pb.PROBE_FAILURE_REASON_PROVIDER_INTERNAL,
            ):
                await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")
            result.failure.CopyFrom(outcome)
        else:
            logger.error("provider control operation returned an invalid result operation=probe")
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")

    async def _set_candidate_verification_outcome(
        self,
        response: pb.VerifyCandidateResponse,
        outcome: CandidateVerificationOutcome | pb.CandidateVerificationUnsupported,
        context: grpc.aio.ServicerContext,
    ) -> None:
        if isinstance(outcome, pb.CandidateVerified):
            response.verified.CopyFrom(outcome)
        elif isinstance(outcome, pb.CandidateRejected):
            if outcome.reason not in (
                pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT,
                pb.CANDIDATE_REJECTION_REASON_MALFORMED,
                pb.CANDIDATE_REJECTION_REASON_NOT_ACCEPTED,
            ):
                await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")
            response.rejected.CopyFrom(outcome)
        elif isinstance(outcome, pb.ReauthenticationRequired):
            response.reauthentication_required.CopyFrom(outcome)
        elif isinstance(outcome, pb.ChallengeRequired):
            response.challenge_required.CopyFrom(outcome)
        elif isinstance(outcome, pb.UpstreamUnavailable):
            response.upstream_unavailable.CopyFrom(outcome)
        elif isinstance(outcome, pb.OperationOutcomeUnknown):
            response.outcome_unknown.CopyFrom(outcome)
        elif isinstance(outcome, pb.CandidateVerificationUnsupported):
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider capability contradicted its result")
        else:
            logger.error("provider control operation returned an invalid result operation=verify_candidate")
            await self._abort(context, grpc.StatusCode.INTERNAL, "provider returned an invalid operation result")


def add_session_provider_control_service(
    server: grpc.aio.Server, provider: ProviderBackend
) -> SessionProviderControlService:
    """Register the control service on a caller-configured, private authenticated server."""
    servicer = SessionProviderControlService(provider)
    pb_grpc.add_SessionProviderControlServicer_to_server(servicer, server)
    return servicer
