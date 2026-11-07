"""Validation of provider control messages at both ends of the RPC boundary."""

import re
from collections.abc import Callable

from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.provider.v1 import session_provider_pb2 as pb
from platform_sessions.routes import Route


class ProviderProtocolError(ValueError):
    """A provider control message violates the protocol."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PROBE_SOURCES = {pb.PROBE_SOURCE_SCHEDULED, pb.PROBE_SOURCE_MANUAL}


def _enum(value: int, name: Callable[[int], str], *, allow_unspecified: bool = False) -> None:
    try:
        if value == 0 and not allow_unspecified:
            raise ValueError("unspecified")
        name(value)
    except ValueError as exc:
        raise ProviderProtocolError("Provider returned an invalid enum value") from exc


def _timestamp(value: Timestamp) -> None:
    try:
        value.ToDatetime()
    except (OverflowError, ValueError) as exc:
        raise ProviderProtocolError("Provider returned an invalid timestamp") from exc


def valid_id(value: str) -> bool:
    return _ID.fullmatch(value) is not None


def provider_info(provider: pb.ProviderInfo) -> None:
    if not valid_id(provider.provider_id) or not provider.display_name or not provider.version:
        raise ProviderProtocolError("Provider info has an invalid provider identity")
    instances: set[str] = set()
    for instance in provider.instances:
        if not valid_id(instance.instance_id) or instance.instance_id in instances:
            raise ProviderProtocolError("Provider info has an invalid instance identity")
        instances.add(instance.instance_id)
        if instance.HasField("openapi_document_path"):
            try:
                Route("GET", instance.openapi_document_path)
            except ValueError as exc:
                raise ProviderProtocolError("Provider info has an invalid OpenAPI path") from exc
        formats: set[tuple[str, str]] = set()
        for session_format in instance.accepted_session_import_formats:
            identity = (session_format.media_type, session_format.version)
            if not all(identity) or identity in formats:
                raise ProviderProtocolError("Provider info has an invalid import format")
            formats.add(identity)
        if instance.supports_candidate_verification and not formats:
            raise ProviderProtocolError("Provider info declares candidate verification without an import format")


def _unknown(value: pb.OperationOutcomeUnknown) -> None:
    _enum(value.reason, pb.UnknownOutcomeReason.Name, allow_unspecified=True)


def probe_result(result: pb.ProbeResult, *, require_metadata: bool = True) -> None:
    if require_metadata:
        if not result.HasField("completed_at") or result.source not in PROBE_SOURCES:
            raise ProviderProtocolError("Provider returned an incomplete probe result")
        _timestamp(result.completed_at)
    kind = result.WhichOneof("result")
    if kind is None:
        raise ProviderProtocolError("Provider returned a probe without a typed result")
    if kind == "failure":
        try:
            _enum(result.failure.reason, pb.ProbeFailureReason.Name)
        except ProviderProtocolError as exc:
            raise ProviderProtocolError("Provider returned a probe without a valid typed result") from exc
    elif kind == "outcome_unknown":
        _unknown(result.outcome_unknown)


def snapshot(value: pb.SessionSnapshot) -> None:
    try:
        _enum(value.state, pb.SessionState.Name)
    except ProviderProtocolError as exc:
        raise ProviderProtocolError("Provider returned an invalid session snapshot") from exc
    if value.state != pb.SESSION_STATE_STORAGE_ERROR and not value.HasField("state_changed_at"):
        raise ProviderProtocolError("Provider returned an incomplete session snapshot")
    for field in ("state_changed_at", "last_verified_at", "imported_at"):
        if value.HasField(field):
            _timestamp(getattr(value, field))
    if value.HasField("last_probe"):
        probe_result(value.last_probe)


def candidate_result(result: pb.ImportSessionResponse | pb.VerifyCandidateResponse) -> None:
    kind = result.WhichOneof("result")
    if kind is None:
        raise ProviderProtocolError("Provider returned a candidate operation without a valid typed result")
    if kind == "rejected":
        try:
            _enum(result.rejected.reason, pb.CandidateRejectionReason.Name)
        except ProviderProtocolError as exc:
            raise ProviderProtocolError("Provider returned a candidate operation without a valid typed result") from exc
    elif kind == "outcome_unknown":
        _unknown(result.outcome_unknown)
    elif kind == "failure" and isinstance(result, pb.ImportSessionResponse):
        failure = result.failure
        _enum(failure.reason, pb.ImportFailureReason.Name)
        if failure.HasField("upstream_http_status") and (
            failure.reason != pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR
            or not 100 <= failure.upstream_http_status <= 599
        ):
            raise ProviderProtocolError("Provider returned an invalid import failure")
