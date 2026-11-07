"""Adapt a remotely owned session to the existing owner management API."""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.host import IntegrationError
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.v1 import session_provider_pb2 as pb
from platform_sessions.registry import IntegrationInstance

logger = logging.getLogger(__name__)

_STATES = {
    pb.SESSION_STATE_NO_SESSION: "uninitialized",
    pb.SESSION_STATE_UNVERIFIED: "not_checked",
    pb.SESSION_STATE_VERIFIED: "ready",
    pb.SESSION_STATE_REAUTHENTICATION_REQUIRED: "reauth_required",
    pb.SESSION_STATE_CHALLENGE_REQUIRED: "challenge",
    pb.SESSION_STATE_OUTCOME_UNKNOWN: "outcome_unknown",
    pb.SESSION_STATE_STORAGE_ERROR: "storage_error",
}
_STATE_MESSAGES = {
    pb.SESSION_STATE_NO_SESSION: "No session configured",
    pb.SESSION_STATE_UNVERIFIED: "Session not yet verified",
    pb.SESSION_STATE_VERIFIED: "Session last known to be verified",
    pb.SESSION_STATE_REAUTHENTICATION_REQUIRED: "Session needs reauthentication",
    pb.SESSION_STATE_CHALLENGE_REQUIRED: "Provider security challenge",
    pb.SESSION_STATE_OUTCOME_UNKNOWN: "Session outcome unknown",
    pb.SESSION_STATE_STORAGE_ERROR: "Could not read session state",
}
_PROBE_FAILURES = {
    pb.PROBE_FAILURE_REASON_INVALID_RESPONSE: "invalid_response",
    pb.PROBE_FAILURE_REASON_UPSTREAM_HTTP_ERROR: "upstream_http_error",
    pb.PROBE_FAILURE_REASON_STORAGE_ERROR: "storage_error",
    pb.PROBE_FAILURE_REASON_SUPERSEDED: "superseded",
    pb.PROBE_FAILURE_REASON_PROVIDER_INTERNAL: "provider_internal",
}
_PROBE_OUTCOMES = {
    "verified": "success",
    "no_session": "uninitialized",
    "reauthentication_required": "reauth_required",
    "challenge_required": "security_challenge",
    "upstream_unavailable": "network_error",
    "outcome_unknown": "outcome_unknown",
}


def _time(value: Timestamp) -> str:
    return value.ToDatetime(tzinfo=datetime.UTC).isoformat()


def _candidate_failure(result: str, reason: int) -> IntegrationError:
    if result == "unsupported":
        return IntegrationError("not_supported", 404)
    if result == "rejected":
        if reason == pb.CANDIDATE_REJECTION_REASON_NOT_ACCEPTED:
            return IntegrationError("verification_failed", 400)
        return IntegrationError("invalid_session", 400)
    if result == "reauthentication_required":
        return IntegrationError("reauth_required", 409)
    if result == "challenge_required":
        return IntegrationError("security_challenge", 503)
    if result == "upstream_unavailable":
        return IntegrationError("upstream_unavailable", 503)
    return IntegrationError("outcome_unknown", 503)


@dataclass
class RemoteIntegration:
    """Management facade; the remote provider alone owns and rotates the session.

    ``candidate_verification`` means this facade implements the endpoint. Actual
    provider support is advertised by GetInfo and enforced by VerifyCandidate.
    """

    config: IntegrationInstance
    client: ProviderControlClient
    periodic: bool = False  # Remote providers own their own background probes.
    candidate_verification: bool = True

    def __post_init__(self) -> None:
        if (self.config.type, self.config.id) != (self.client.instance.provider_id, self.client.instance.instance_id):
            raise ValueError("Remote integration and control client identity differ")

    async def metadata(self) -> dict:
        result = {"id": self.config.id, "type": self.config.type, "instructions": [], "candidate_verification": False}
        try:
            info = await self.client.get_info()
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log_unavailable("GetInfo", exc)
            return result
        instance = next(item for item in info.provider.instances if item.instance_id == self.config.id)
        result["instructions"] = [
            {"text": step.text, **({"command": step.command} if step.HasField("command") else {})}
            for step in instance.instructions
        ]
        result["candidate_verification"] = instance.supports_candidate_verification
        return result

    def parse(self, body: bytes) -> bytes:
        """Validate the shared JSON envelope; send original bytes for provider validation."""
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "provider", "session"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or payload["provider"] != self.config.type
            or not isinstance(payload["session"], dict)
        ):
            raise ValueError("Invalid session export")
        return body

    async def status(self) -> dict:
        try:
            snapshot = (await self.client.get_session()).snapshot
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log_unavailable("GetSession", exc)
            return {
                "state": "upstream_unavailable",
                "outcome": "not_checked",
                "message": "Provider service unavailable",
                "details": [],
            }
        outcome = "uninitialized" if snapshot.state == pb.SESSION_STATE_NO_SESSION else "not_checked"
        checked_at = None
        age_seconds = None
        if snapshot.HasField("last_probe"):
            probe = snapshot.last_probe
            kind = probe.WhichOneof("result")
            outcome = _PROBE_FAILURES[probe.failure.reason] if kind == "failure" else _PROBE_OUTCOMES[kind]
            checked_at = _time(probe.completed_at)
            if snapshot.HasField("imported_at"):
                age_seconds = max(
                    0, int((probe.completed_at.ToDatetime() - snapshot.imported_at.ToDatetime()).total_seconds())
                )
        result = {
            "state": _STATES[snapshot.state],
            "outcome": outcome,
            "message": _STATE_MESSAGES[snapshot.state],
            "details": [
                {
                    "key": "imported_at",
                    "value": _time(snapshot.imported_at) if snapshot.HasField("imported_at") else None,
                    "format": "time",
                },
                {"key": "age_seconds", "value": age_seconds, "format": "duration"},
                {"key": "checked_at", "value": checked_at, "format": "time"},
                {"key": "outcome", "value": outcome},
            ],
        }
        if snapshot.HasField("owner_presentation"):
            result["message"] = snapshot.owner_presentation.message
            if snapshot.owner_presentation.HasField("help"):
                result["help"] = snapshot.owner_presentation.help
        return result

    async def refresh(self, transport=None, *, source: str = "verify") -> None:
        # The host's verify route calls this once and then reads status. The provider
        # has already persisted a completed probe before returning its typed result.
        probe_source = pb.PROBE_SOURCE_SCHEDULED if source == "periodic" else pb.PROBE_SOURCE_MANUAL
        try:
            await self.client.probe(source=probe_source)
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log_unavailable("Probe", exc)
            raise IntegrationError("outcome_unknown", 503) from None

    async def check_candidate(self, candidate: bytes, transport=None) -> None:
        try:
            response = await self.client.verify_candidate(candidate, media_type="application/json", format_version="1")
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log_unavailable("VerifyCandidate", exc)
            raise IntegrationError("outcome_unknown", 503) from None
        if response.WhichOneof("result") != "verified":
            raise _candidate_failure(response.WhichOneof("result"), response.rejected.reason)

    async def install(self, candidate: bytes, transport=None) -> dict:
        try:
            response = await self.client.import_session(candidate, media_type="application/json", format_version="1")
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log_unavailable("ImportSession", exc)
            raise IntegrationError("outcome_unknown", 503) from None
        result = response.WhichOneof("result")
        if result == "failure":
            if response.failure.reason == pb.IMPORT_FAILURE_REASON_INVALID_RESPONSE:
                raise IntegrationError("invalid_response", 502)
            if response.failure.reason == pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR:
                if response.failure.HasField("upstream_http_status"):
                    raise IntegrationError(f"upstream_http_{response.failure.upstream_http_status}", 502)
                raise IntegrationError("upstream_unavailable", 503)
            raise IntegrationError("storage_error", 503)
        if result == "outcome_unknown":
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_TIMEOUT:
                raise IntegrationError("verification_timeout", 504)
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_UNREACHABLE:
                raise IntegrationError("upstream_unavailable", 503)
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE:
                raise IntegrationError("outcome_unknown_invalid_response", 502)
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR:
                if response.outcome_unknown.HasField("upstream_http_status"):
                    raise IntegrationError(f"outcome_unknown_http_{response.outcome_unknown.upstream_http_status}", 502)
                raise IntegrationError("outcome_unknown_http", 502)
        if result != "installed":
            raise _candidate_failure(result, response.rejected.reason)
        return {"configured": True}

    def _log_unavailable(self, operation: str, exc: Exception) -> None:
        # gRPC details and exception messages can include credentials. Log categories only.
        logger.warning(
            "Provider control operation unavailable operation=%s provider_id=%s instance_id=%s error_type=%s",
            operation,
            self.config.type,
            self.config.id,
            type(exc).__name__,
        )
