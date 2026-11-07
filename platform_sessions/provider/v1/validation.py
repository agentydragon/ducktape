"""Validation of provider control messages at both ends of the RPC boundary."""

import re

from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.provider.v1 import session_provider_pb2 as pb
from platform_sessions.routes import Route


class ProviderProtocolError(ValueError):
    """A provider control message violates the protocol."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PROBE_SOURCES = {pb.PROBE_SOURCE_SCHEDULED, pb.PROBE_SOURCE_MANUAL}


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


def probe_result(result: pb.ProbeResult, *, require_metadata: bool = True) -> None:
    if require_metadata:
        if not result.HasField("completed_at") or result.source not in PROBE_SOURCES:
            raise ProviderProtocolError("Provider returned an incomplete probe result")
        _timestamp(result.completed_at)
    if result.outcome == pb.PROBE_OUTCOME_UNSPECIFIED:
        raise ProviderProtocolError("Provider returned a probe without an outcome")
    if result.outcome == pb.PROBE_OUTCOME_UNKNOWN and not result.HasField("outcome_unknown"):
        raise ProviderProtocolError("Unknown probe outcome lacks rotation details")
    if result.outcome == pb.PROBE_OUTCOME_FAILURE and (
        not result.HasField("failure") or result.failure.reason == pb.PROBE_FAILURE_REASON_UNSPECIFIED
    ):
        raise ProviderProtocolError("Probe failure lacks a reason")


def snapshot(value: pb.SessionSnapshot) -> None:
    if value.state == pb.SESSION_STATE_UNSPECIFIED:
        raise ProviderProtocolError("Provider returned an invalid session snapshot")
    if value.state != pb.SESSION_STATE_STORAGE_ERROR and not value.HasField("state_changed_at"):
        raise ProviderProtocolError("Provider returned an incomplete session snapshot")
    for field in ("state_changed_at", "last_verified_at", "imported_at"):
        if value.HasField(field):
            _timestamp(getattr(value, field))
    if value.HasField("last_probe"):
        probe_result(value.last_probe)


def candidate_result(result: pb.ImportSessionResponse | pb.VerifyCandidateResponse) -> None:
    if isinstance(result, pb.ImportSessionResponse):
        unspecified = result.outcome == pb.IMPORT_OUTCOME_UNSPECIFIED
        rejected = result.outcome == pb.IMPORT_OUTCOME_REJECTED
        unknown = result.outcome == pb.IMPORT_OUTCOME_UNKNOWN
        if result.outcome == pb.IMPORT_OUTCOME_FAILURE:
            if not result.HasField("failure") or result.failure.reason == pb.IMPORT_FAILURE_REASON_UNSPECIFIED:
                raise ProviderProtocolError("Import failure lacks a reason")
            failure = result.failure
            _check_status(
                failure.reason,
                failure.upstream_http_status if failure.HasField("upstream_http_status") else None,
                pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR,
            )
    else:
        unspecified = result.outcome == pb.VERIFY_OUTCOME_UNSPECIFIED
        rejected = result.outcome == pb.VERIFY_OUTCOME_REJECTED
        unknown = result.outcome == pb.VERIFY_OUTCOME_UNKNOWN
    if unspecified:
        raise ProviderProtocolError("Candidate operation has no outcome")
    if rejected and (
        not result.HasField("rejected") or result.rejected.reason == pb.CANDIDATE_REJECTION_REASON_UNSPECIFIED
    ):
        raise ProviderProtocolError("Candidate rejection lacks a reason")
    if unknown:
        if not result.HasField("outcome_unknown"):
            raise ProviderProtocolError("Unknown outcome lacks rotation details")
        detail = result.outcome_unknown
        _check_status(
            detail.reason,
            detail.upstream_http_status if detail.HasField("upstream_http_status") else None,
            pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR,
        )


def _check_status(reason: int, status: int | None, upstream_reason: int) -> None:
    if status is not None and (reason != upstream_reason or not 100 <= status <= 599):
        raise ProviderProtocolError("Invalid upstream status")
