"""Gateway client for one remotely owned platform session.

The caller owns the gRPC channel. Use ``dial_provider_control`` for a TLS
channel without automatic retries; any other channel constructor must likewise
disable gRPC retries. A lost RPC response is not evidence that an import or
probe did not run upstream.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from pathlib import Path

import grpc

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as rpc, validation
from platform_sessions.provider.v1.validation import ProviderProtocolError


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
        if not validation.valid_id(provider_id) or not validation.valid_id(instance_id):
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
    def _check_candidate(candidate: bytes, media_type: str, format_version: str) -> None:
        if (
            not isinstance(candidate, bytes)
            or not candidate
            or len(candidate) > MAX_SESSION_BYTES
            or not media_type
            or not format_version
        ):
            raise ValueError("Invalid session candidate request")

    async def get_info(self) -> pb.GetInfoResponse:
        response = await self.stub.GetInfo(
            pb.GetInfoRequest(), metadata=await self._metadata(), timeout=self.timeout_s, wait_for_ready=False
        )
        if not response.HasField("provider") or response.provider.provider_id != self.instance.provider_id:
            raise ProviderProtocolError("Provider info has the wrong provider identity")
        validation.provider_info(response.provider)
        if self.instance.instance_id not in {item.instance_id for item in response.provider.instances}:
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
        if not response.HasField("snapshot"):
            raise ProviderProtocolError("Provider returned an invalid session snapshot")
        validation.snapshot(response.snapshot)
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
        validation.candidate_result(response)
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
        validation.candidate_result(response)
        return response

    async def probe(self, *, source: pb.ProbeSource) -> pb.ProbeResponse:
        if source not in validation.PROBE_SOURCES:
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
        validation.probe_result(response.result)
        if response.result.source != source:
            raise ProviderProtocolError("Provider returned a probe for the wrong source")
        return response
