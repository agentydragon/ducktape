"""Owner management routes that call remote provider control services."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging

import grpc
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.auth import BearerAuth
from platform_sessions.host import IntegrationError, owner_authorizer, same_origin
from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.v1 import session_provider_pb2 as pb

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


def _log_unavailable(client: ProviderControlClient, operation: str, exc: Exception) -> None:
    logger.warning(
        "Provider control operation unavailable operation=%s provider_id=%s instance_id=%s error_type=%s",
        operation,
        client.instance.provider_id,
        client.instance.instance_id,
        type(exc).__name__,
    )


async def _metadata(client: ProviderControlClient) -> dict:
    result = {
        "id": client.instance.instance_id,
        "type": client.instance.provider_id,
        "instructions": [],
        "candidate_verification": False,
    }
    try:
        info = await client.get_info()
    except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
        _log_unavailable(client, "GetInfo", exc)
        return result
    instance = next(item for item in info.provider.instances if item.instance_id == client.instance.instance_id)
    result["instructions"] = [
        {"text": step.text, **({"command": step.command} if step.HasField("command") else {})}
        for step in instance.instructions
    ]
    result["candidate_verification"] = instance.supports_candidate_verification
    return result


async def _status(client: ProviderControlClient) -> dict:
    try:
        snapshot = (await client.get_session()).snapshot
    except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
        _log_unavailable(client, "GetSession", exc)
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


def create_remote_management_router(
    clients: dict[str, ProviderControlClient], public_base_url: str, *, require_owner, auth: BearerAuth
) -> APIRouter:
    """Serve owner operations by calling the configured remote providers directly."""
    if any(key != client.instance.instance_id for key, client in clients.items()):
        raise ValueError("Provider client identity differs from its configured instance")
    router = APIRouter(prefix="/api/integrations", dependencies=[Depends(owner_authorizer(require_owner, auth))])

    async def candidate_request(id: str, request: Request):
        client = clients.get(id)
        if client is None:
            return None, None, JSONResponse({"error": "not_found"}, status_code=404)
        if not same_origin(request, public_base_url, auth):
            return None, None, JSONResponse({"error": "invalid_origin"}, status_code=403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_SESSION_BYTES:
                return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
            body.extend(chunk)
        try:
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "provider", "session"}
                or type(payload["version"]) is not int
                or payload["version"] != 1
                or payload["provider"] != client.instance.provider_id
                or not isinstance(payload["session"], dict)
            ):
                raise ValueError("Invalid session export")
        except ValueError, TypeError, UnicodeError:
            return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
        return client, bytes(body), None

    @router.get("")
    async def list_integrations():
        return await asyncio.gather(*(_metadata(client) for client in clients.values()))

    @router.get("/{id}")
    async def get_integration(id: str):
        client = clients.get(id)
        if client is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return await _status(client)

    @router.post("/{id}/verify")
    async def verify_installed(id: str, request: Request):
        client = clients.get(id)
        if client is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not same_origin(request, public_base_url, auth):
            return JSONResponse({"error": "invalid_origin"}, status_code=403)
        try:
            await client.probe(source=pb.PROBE_SOURCE_MANUAL)
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            _log_unavailable(client, "Probe", exc)
            return JSONResponse({"error": "outcome_unknown"}, status_code=503)
        return await _status(client)

    @router.post("/{id}/session/verify")
    async def verify_candidate(id: str, request: Request):
        client, candidate, error = await candidate_request(id, request)
        if error is not None:
            return error
        try:
            response = await client.verify_candidate(candidate, media_type="application/json", format_version="1")
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                return JSONResponse({"error": "not_supported"}, status_code=404)
            _log_unavailable(client, "VerifyCandidate", exc)
            return JSONResponse({"error": "outcome_unknown"}, status_code=503)
        except (OSError, ValueError) as exc:
            _log_unavailable(client, "VerifyCandidate", exc)
            return JSONResponse({"error": "outcome_unknown"}, status_code=503)
        result = response.WhichOneof("result")
        if result != "verified":
            failure = _candidate_failure(result, response.rejected.reason)
            return JSONResponse({"error": failure.code}, status_code=failure.status)
        return {"outcome": "success"}

    @router.post("/{id}/session")
    async def import_session(id: str, request: Request):
        client, candidate, error = await candidate_request(id, request)
        if error is not None:
            return error
        try:
            response = await client.import_session(candidate, media_type="application/json", format_version="1")
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            _log_unavailable(client, "ImportSession", exc)
            return JSONResponse({"error": "outcome_unknown"}, status_code=503)
        result = response.WhichOneof("result")
        if result == "failure":
            if response.failure.reason == pb.IMPORT_FAILURE_REASON_INVALID_RESPONSE:
                return JSONResponse({"error": "invalid_response"}, status_code=502)
            if response.failure.reason == pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR:
                if response.failure.HasField("upstream_http_status"):
                    return JSONResponse(
                        {"error": f"upstream_http_{response.failure.upstream_http_status}"}, status_code=502
                    )
                return JSONResponse({"error": "upstream_unavailable"}, status_code=503)
            return JSONResponse({"error": "storage_error"}, status_code=503)
        if result == "outcome_unknown":
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_TIMEOUT:
                return JSONResponse({"error": "verification_timeout"}, status_code=504)
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_UNREACHABLE:
                return JSONResponse({"error": "upstream_unavailable"}, status_code=503)
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE:
                return JSONResponse({"error": "outcome_unknown_invalid_response"}, status_code=502)
            if response.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR:
                if response.outcome_unknown.HasField("upstream_http_status"):
                    return JSONResponse(
                        {"error": f"outcome_unknown_http_{response.outcome_unknown.upstream_http_status}"},
                        status_code=502,
                    )
                return JSONResponse({"error": "outcome_unknown_http"}, status_code=502)
        if result != "installed":
            failure = _candidate_failure(result, response.rejected.reason)
            return JSONResponse({"error": failure.code}, status_code=failure.status)
        return {"configured": True}

    return router
