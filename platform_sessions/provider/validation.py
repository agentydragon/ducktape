"""Validation of provider control messages at both ends of the RPC boundary."""

import re

from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.provider import session_provider_pb2 as pb
from platform_sessions.routes import Route


class ProviderProtocolError(ValueError):
    """A provider control message violates the protocol."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PROBE_SOURCES = {
    pb.PROBE_SOURCE_SCHEDULED,
    pb.PROBE_SOURCE_MANUAL,
    pb.PROBE_SOURCE_VERIFY,
    pb.PROBE_SOURCE_STARTUP,
    pb.PROBE_SOURCE_OTHER,
}


def _timestamp(value: Timestamp) -> None:
    try:
        value.ToDatetime()
    except (OverflowError, ValueError) as exc:
        raise ProviderProtocolError("Provider returned an invalid timestamp") from exc


def valid_id(value: str) -> bool:
    return _ID.fullmatch(value) is not None


def provider_info(provider: pb.ProviderInfo) -> None:
    if not valid_id(provider.provider_id) or not provider.display_name:
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
        formats: set[str] = set()
        for session_format in instance.accepted_session_import_formats:
            identity = session_format.media_type
            if not identity or identity in formats:
                raise ProviderProtocolError("Provider info has an invalid import format")
            formats.add(identity)


def probe_result(result: pb.ProbeResult, *, require_metadata: bool = True) -> None:
    if require_metadata:
        if not result.HasField("completed_at") or result.source not in PROBE_SOURCES:
            raise ProviderProtocolError("Provider returned an incomplete probe result")
        _timestamp(result.completed_at)
    outcome = result.WhichOneof("outcome")
    if outcome is None:
        raise ProviderProtocolError("Provider returned a probe without an outcome")
    if outcome == "outcome_unknown":
        detail = result.outcome_unknown
        _check_status(
            detail.reason,
            detail.upstream_http_status if detail.HasField("upstream_http_status") else None,
            pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR,
        )
    if outcome == "failure" and (
        not result.HasField("failure") or result.failure.reason == pb.PROBE_FAILURE_REASON_UNSPECIFIED
    ):
        raise ProviderProtocolError("Probe failure lacks a reason")


def snapshot(value: pb.SessionSnapshot) -> None:
    status = value.WhichOneof("status")
    if status == "known":
        if value.known.state == pb.SESSION_STATE_UNSPECIFIED or not value.known.HasField("state_changed_at"):
            raise ProviderProtocolError("Provider returned an incomplete session snapshot")
        _timestamp(value.known.state_changed_at)
    elif status == "storage_error":
        if value.storage_error.HasField("state_changed_at"):
            _timestamp(value.storage_error.state_changed_at)
    else:
        raise ProviderProtocolError("Provider returned an invalid session snapshot")
    if value.HasField("last_verified_at"):
        _timestamp(value.last_verified_at)
    if value.HasField("imported_at"):
        _timestamp(value.imported_at)
    if value.HasField("last_probe"):
        probe_result(value.last_probe)


def import_result(result: pb.ImportSessionResponse) -> None:
    outcome = result.WhichOneof("outcome")
    if outcome is None:
        raise ProviderProtocolError("Import operation has no outcome")
    if outcome == "failure":
        if result.failure.reason == pb.IMPORT_FAILURE_REASON_UNSPECIFIED:
            raise ProviderProtocolError("Import failure lacks a reason")
        failure = result.failure
        _check_status(
            failure.reason,
            failure.upstream_http_status if failure.HasField("upstream_http_status") else None,
            pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR,
        )
    if outcome == "rejected" and result.rejected.reason == pb.CANDIDATE_REJECTION_REASON_UNSPECIFIED:
        raise ProviderProtocolError("Candidate rejection lacks a reason")
    if outcome == "outcome_unknown":
        detail = result.outcome_unknown
        _check_status(
            detail.reason,
            detail.upstream_http_status if detail.HasField("upstream_http_status") else None,
            pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR,
        )


def _check_status(reason: int, status: int | None, upstream_reason: int) -> None:
    if status is not None and (reason != upstream_reason or not 100 <= status <= 599):
        raise ProviderProtocolError("Invalid upstream status")
