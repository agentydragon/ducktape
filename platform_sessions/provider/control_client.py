"""Gateway client for one remotely owned platform session.

The caller owns the gRPC channel. Use ``dial_provider_control`` for a TLS
channel without automatic retries; any other channel constructor must likewise
disable gRPC retries. A lost RPC response is not evidence that an import or
probe did not run upstream.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence
from pathlib import Path

import grpc

from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as rpc
from platform_sessions.routes import Route, _segments

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PROBE_SOURCES = {pb.PROBE_SOURCE_SCHEDULED, pb.PROBE_SOURCE_MANUAL}
_SESSION_STATES = {
    pb.SESSION_STATE_NO_SESSION,
    pb.SESSION_STATE_UNVERIFIED,
    pb.SESSION_STATE_VERIFIED,
    pb.SESSION_STATE_REAUTHENTICATION_REQUIRED,
    pb.SESSION_STATE_CHALLENGE_REQUIRED,
    pb.SESSION_STATE_OUTCOME_UNKNOWN,
}
_ROUTE_CLASSES = {
    pb.ROUTE_ACCESS_CLASS_READ_ONLY,
    pb.ROUTE_ACCESS_CLASS_STATE_CHANGING,
    pb.ROUTE_ACCESS_CLASS_MIXED_OR_UNKNOWN,
}
_REJECTION_REASONS = {
    pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT,
    pb.CANDIDATE_REJECTION_REASON_MALFORMED,
    pb.CANDIDATE_REJECTION_REASON_NOT_ACCEPTED,
}
_PROBE_FAILURE_REASONS = {
    pb.PROBE_FAILURE_REASON_INVALID_RESPONSE,
    pb.PROBE_FAILURE_REASON_UPSTREAM_HTTP_ERROR,
    pb.PROBE_FAILURE_REASON_STORAGE_ERROR,
    pb.PROBE_FAILURE_REASON_SUPERSEDED,
    pb.PROBE_FAILURE_REASON_PROVIDER_INTERNAL,
}


class ProviderProtocolError(ValueError):
    """A completed RPC response violates the provider control contract."""


def dial_provider_control(
    target: str, *, credentials: grpc.ChannelCredentials | None = None, options: Sequence[tuple[str, int | str]] = ()
) -> grpc.aio.Channel:
    """Create a caller-owned TLS channel with gRPC retries disabled.

    Supply custom TLS credentials for a private CA or mutual TLS. Callers must
    close the returned channel. A separately constructed channel must also set
    ``grpc.enable_retries=0``: a lost import or probe response cannot be replayed.
    """
    if not target:
        raise ValueError("Provider control target is required")
    if any(name == "grpc.enable_retries" for name, _ in options):
        raise ValueError("Provider control retry option cannot be overridden")
    return grpc.aio.secure_channel(
        target,
        credentials if credentials is not None else grpc.ssl_channel_credentials(),
        options=(("grpc.enable_retries", 0), *options),
    )


class ProviderControlClient:
    """Call one provider instance without owning or closing its channel.

    The projected bearer is read afresh for each RPC so a rotated file is used
    without rebuilding the channel. gRPC transport/status errors propagate as
    errors; they are never converted into a completed provider result or retried.
    """

    def __init__(
        self, channel: grpc.aio.Channel, *, provider_id: str, instance_id: str, token_file: Path, timeout_s: float
    ) -> None:
        if not _ID.fullmatch(provider_id) or not _ID.fullmatch(instance_id):
            raise ValueError("Invalid provider or instance ID")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("RPC timeout must be positive")
        self.instance = pb.InstanceRef(provider_id=provider_id, instance_id=instance_id)
        self.token_file = token_file
        self.timeout_s = timeout_s
        self.stub = rpc.SessionProviderControlStub(channel)

    async def _metadata(self) -> tuple[tuple[str, str], ...]:
        # This happens before dispatch. Never include the token in an error.
        token = (await asyncio.to_thread(self.token_file.read_text)).strip()
        if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("Provider control bearer is invalid")
        return (("authorization", f"Bearer {token}"),)

    def _check_instance(self, returned: pb.InstanceRef) -> None:
        if returned != self.instance:
            raise ProviderProtocolError("Provider response has the wrong instance identity")

    @staticmethod
    def _check_timestamp(timestamp) -> None:
        try:
            timestamp.ToDatetime()
        except (OverflowError, ValueError) as exc:
            raise ProviderProtocolError("Provider returned an invalid timestamp") from exc

    @staticmethod
    def _check_probe_result(result: pb.ProbeResult) -> None:
        if not result.HasField("completed_at") or result.source not in _PROBE_SOURCES:
            raise ProviderProtocolError("Provider returned an incomplete probe result")
        ProviderControlClient._check_timestamp(result.completed_at)
        kind = result.WhichOneof("result")
        if kind is None or (kind == "failure" and result.failure.reason not in _PROBE_FAILURE_REASONS):
            raise ProviderProtocolError("Provider returned a probe without a typed result")

    @staticmethod
    def _check_candidate(candidate: bytes, media_type: str, format_version: str) -> None:
        if (
            not isinstance(candidate, bytes)
            or not candidate
            or len(candidate) > 65_536
            or not media_type
            or not format_version
        ):
            raise ValueError("Invalid session candidate request")

    @staticmethod
    def _check_candidate_result(result: str | None, rejection_reason: int) -> None:
        if result is None or (result == "rejected" and rejection_reason not in _REJECTION_REASONS):
            raise ProviderProtocolError("Provider returned a candidate operation without a valid typed result")

    async def get_info(self) -> pb.GetInfoResponse:
        response = await self.stub.GetInfo(
            pb.GetInfoRequest(), metadata=await self._metadata(), timeout=self.timeout_s, wait_for_ready=False
        )
        if not response.HasField("provider") or response.provider.provider_id != self.instance.provider_id:
            raise ProviderProtocolError("Provider info has the wrong provider identity")
        seen_instances: set[str] = set()
        for info in response.provider.instances:
            if not _ID.fullmatch(info.instance_id) or info.instance_id in seen_instances:
                raise ProviderProtocolError("Provider info has an invalid instance identity")
            seen_instances.add(info.instance_id)
            if info.HasField("openapi_document_path"):
                try:
                    _segments(info.openapi_document_path)
                except ValueError as exc:
                    raise ProviderProtocolError("Provider info has an invalid OpenAPI path") from exc
            seen_operations: set[str] = set()
            for route in info.routes:
                if (
                    not _ID.fullmatch(route.operation_id)
                    or route.operation_id in seen_operations
                    or route.access_class not in _ROUTE_CLASSES
                ):
                    raise ProviderProtocolError("Provider info has an invalid route declaration")
                try:
                    Route(route.method, route.path_template)
                except ValueError as exc:
                    raise ProviderProtocolError("Provider info has an invalid route declaration") from exc
                seen_operations.add(route.operation_id)
            seen_formats: set[tuple[str, str]] = set()
            for session_format in info.accepted_session_import_formats:
                identity = (session_format.media_type, session_format.version)
                if not all(identity) or identity in seen_formats:
                    raise ProviderProtocolError("Provider info has an invalid import format")
                seen_formats.add(identity)
        if self.instance.instance_id not in seen_instances:
            raise ProviderProtocolError("Provider info omits the configured instance")
        return response

    async def get_session(self) -> pb.GetSessionResponse:
        response = await self.stub.GetSession(
            pb.GetSessionRequest(instance=self.instance),
            metadata=await self._metadata(),
            timeout=self.timeout_s,
            wait_for_ready=False,
        )
        self._check_instance(response.instance)
        if not response.HasField("snapshot") or response.snapshot.state not in _SESSION_STATES:
            raise ProviderProtocolError("Provider returned an invalid session snapshot")
        if not response.snapshot.HasField("state_changed_at"):
            raise ProviderProtocolError("Provider returned an incomplete session snapshot")
        self._check_timestamp(response.snapshot.state_changed_at)
        if response.snapshot.HasField("last_verified_at"):
            self._check_timestamp(response.snapshot.last_verified_at)
        if response.snapshot.HasField("last_probe"):
            self._check_probe_result(response.snapshot.last_probe)
        return response

    async def import_session(
        self, candidate: bytes, *, media_type: str, format_version: str
    ) -> pb.ImportSessionResponse:
        self._check_candidate(candidate, media_type, format_version)
        response = await self.stub.ImportSession(
            pb.ImportSessionRequest(
                instance=self.instance, media_type=media_type, format_version=format_version, candidate=candidate
            ),
            metadata=await self._metadata(),
            timeout=self.timeout_s,
            wait_for_ready=False,
        )
        self._check_instance(response.instance)
        self._check_candidate_result(response.WhichOneof("result"), response.rejected.reason)
        return response

    async def verify_candidate(
        self, candidate: bytes, *, media_type: str, format_version: str
    ) -> pb.VerifyCandidateResponse:
        """Check a candidate without installing it; never replay an ambiguous call."""
        self._check_candidate(candidate, media_type, format_version)
        response = await self.stub.VerifyCandidate(
            pb.VerifyCandidateRequest(
                instance=self.instance, media_type=media_type, format_version=format_version, candidate=candidate
            ),
            metadata=await self._metadata(),
            timeout=self.timeout_s,
            wait_for_ready=False,
        )
        self._check_instance(response.instance)
        self._check_candidate_result(response.WhichOneof("result"), response.rejected.reason)
        return response

    async def probe(self, *, source: int) -> pb.ProbeResponse:
        if source not in _PROBE_SOURCES:
            raise ValueError("Invalid probe source")
        response = await self.stub.Probe(
            pb.ProbeRequest(instance=self.instance, source=source),
            metadata=await self._metadata(),
            timeout=self.timeout_s,
            wait_for_ready=False,
        )
        self._check_instance(response.instance)
        if not response.HasField("result"):
            raise ProviderProtocolError("Provider returned an incomplete probe response")
        self._check_probe_result(response.result)
        if response.result.source != source:
            raise ProviderProtocolError("Provider returned a probe for the wrong source")
        return response
