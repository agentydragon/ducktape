"""The gateway control client talks to a real in-process gRPC server."""

from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from typing import override

import grpc
import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.provider.control_client import (
    ProviderControlClient,
    ProviderProtocolError,
    dial_provider_control,
)
from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as rpc

INSTANCE = pb.InstanceRef(provider_id="example", instance_id="account")


def _now() -> Timestamp:
    return Timestamp(seconds=1_700_000_000)


class Provider(rpc.SessionProviderControlServicer):
    def __init__(self):
        self.calls: Counter[str] = Counter()
        self.tokens: list[str] = []
        self.candidates: list[bytes] = []
        self.info = pb.GetInfoResponse(
            provider=pb.ProviderInfo(
                provider_id="example",
                display_name="Example",
                version="test",
                instances=[
                    pb.ProviderInstanceInfo(
                        instance_id="account",
                        supports_candidate_verification=True,
                        instructions=[
                            pb.ExportInstruction(text="Export the browser session.", command="example export")
                        ],
                        accepted_session_import_formats=[
                            pb.SessionImportFormat(media_type="application/json", version="1")
                        ],
                    )
                ],
            )
        )
        self.session = pb.GetSessionResponse(
            instance=INSTANCE, snapshot=pb.SessionSnapshot(state=pb.SESSION_STATE_VERIFIED, state_changed_at=_now())
        )
        self.import_result = pb.ImportSessionResponse(instance=INSTANCE, installed=pb.SessionInstalled())
        self.verify_result = pb.VerifyCandidateResponse(instance=INSTANCE, verified=pb.CandidateVerified())
        self.probe_result = pb.ProbeResponse(
            instance=INSTANCE,
            result=pb.ProbeResult(completed_at=_now(), source=pb.PROBE_SOURCE_MANUAL, verified=pb.ProbeVerified()),
        )
        self.abort_import = False
        self.abort_verify = False
        self.unsupported_verify = False

    def _record(self, method, context):
        self.calls[method] += 1
        metadata = dict(context.invocation_metadata())
        self.tokens.append(metadata.get("authorization", ""))

    @override
    async def GetInfo(self, request, context):
        self._record("GetInfo", context)
        return self.info

    @override
    async def GetSession(self, request, context):
        self._record("GetSession", context)
        assert request.instance == INSTANCE
        return self.session

    @override
    async def ImportSession(self, request, context):
        self._record("ImportSession", context)
        assert request.instance == INSTANCE
        assert (request.media_type, request.format_version) == ("application/json", "1")
        self.candidates.append(request.candidate)
        if self.abort_import:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "provider response lost")
        return self.import_result

    @override
    async def Probe(self, request, context):
        self._record("Probe", context)
        assert request.instance == INSTANCE
        return self.probe_result

    @override
    async def VerifyCandidate(self, request, context):
        self._record("VerifyCandidate", context)
        assert request.instance == INSTANCE
        self.candidates.append(request.candidate)
        if self.unsupported_verify:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "candidate verification unavailable")
        if self.abort_verify:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "provider response lost")
        return self.verify_result


@asynccontextmanager
async def served(tmp_path):
    provider = Provider()
    server = grpc.aio.server()
    rpc.add_SessionProviderControlServicer_to_server(provider, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    token_file = tmp_path / "gateway-bearer"
    token_file.write_text("a" * 32 + "\n")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=(("grpc.enable_retries", 0),))
    client = ProviderControlClient(
        channel, provider_id="example", instance_id="account", token_file=token_file, timeout_s=2
    )
    try:
        yield provider, client, token_file, channel
    finally:
        await channel.close()
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_round_trip_typed_results_and_rotated_projected_bearer(tmp_path):
    async with served(tmp_path) as (provider, client, token_file, channel):
        assert (await client.get_info()).provider.instances[0].instance_id == "account"
        token_file.write_text("b" * 32 + "\n")
        assert (await client.get_session()).snapshot.state == pb.SESSION_STATE_VERIFIED
        candidate = b'{"version":1,"session":{"token":"secret"}}'
        assert (await client.import_session(candidate, media_type="application/json", format_version="1")).WhichOneof(
            "result"
        ) == "installed"
        assert (await client.probe(source=pb.PROBE_SOURCE_MANUAL)).result.WhichOneof("result") == "verified"
        assert (await client.verify_candidate(candidate, media_type="application/json", format_version="1")).WhichOneof(
            "result"
        ) == "verified"
        assert provider.candidates == [candidate, candidate]
        assert provider.tokens == ["Bearer " + "a" * 32, *("Bearer " + "b" * 32,) * 4]
        assert provider.calls == Counter(GetInfo=1, GetSession=1, ImportSession=1, Probe=1, VerifyCandidate=1)
        # The client leaves its injected channel available to its owner.
        assert (
            await rpc.SessionProviderControlStub(channel).GetInfo(pb.GetInfoRequest())
        ).provider.provider_id == "example"


@pytest.mark.asyncio
async def test_typed_provider_failures_stay_distinct_from_rpc_failure(tmp_path):
    async with served(tmp_path) as (provider, client, _, _):
        provider.import_result = pb.ImportSessionResponse(
            instance=INSTANCE, rejected=pb.CandidateRejected(reason=pb.CANDIDATE_REJECTION_REASON_NOT_ACCEPTED)
        )
        provider.probe_result.result.ClearField("verified")
        provider.probe_result.result.outcome_unknown.credential_rotation_may_have_occurred = True
        assert (
            await client.import_session(b"opaque-secret", media_type="application/json", format_version="1")
        ).WhichOneof("result") == "rejected"
        result = await client.probe(source=pb.PROBE_SOURCE_MANUAL)
        assert result.result.WhichOneof("result") == "outcome_unknown"
        assert result.result.outcome_unknown.credential_rotation_may_have_occurred
        provider.abort_import = True
        with pytest.raises(grpc.aio.AioRpcError) as failure:
            await client.import_session(b"opaque-secret", media_type="application/json", format_version="1")
        assert failure.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert provider.calls["ImportSession"] == 2  # one completed call and exactly one aborted call
        provider.unsupported_verify = True
        with pytest.raises(grpc.aio.AioRpcError) as unsupported:
            await client.verify_candidate(b"opaque-secret", media_type="application/json", format_version="1")
        assert unsupported.value.code() == grpc.StatusCode.UNIMPLEMENTED
        provider.unsupported_verify = False
        provider.abort_verify = True
        with pytest.raises(grpc.aio.AioRpcError) as verify_failure:
            await client.verify_candidate(b"opaque-secret", media_type="application/json", format_version="1")
        assert verify_failure.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert provider.calls["VerifyCandidate"] == 2


@pytest.mark.asyncio
async def test_import_rejects_malformed_provider_diagnostics(tmp_path):
    async with served(tmp_path) as (provider, client, _, _):
        for failure in (
            pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_UNSPECIFIED),
            pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_INVALID_RESPONSE, upstream_http_status=429),
            pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR, upstream_http_status=999),
        ):
            provider.import_result = pb.ImportSessionResponse(instance=INSTANCE, failure=failure)
            with pytest.raises(ProviderProtocolError):
                await client.import_session(b"opaque-secret", media_type="application/json", format_version="1")


@pytest.mark.asyncio
async def test_protocol_errors_reject_missing_and_mismatched_responses_without_candidate_data(tmp_path):
    async with served(tmp_path) as (provider, client, _, _):
        provider.info.provider.instances.append(pb.ProviderInstanceInfo(instance_id="second"))
        assert (await client.get_info()).provider.instances[1].instance_id == "second"
        provider.info.provider.provider_id = "other"
        with pytest.raises(ProviderProtocolError, match="provider identity"):
            await client.get_info()
        provider.info.provider.provider_id = "example"
        provider.session.snapshot.state = pb.SESSION_STATE_UNSPECIFIED
        with pytest.raises(ProviderProtocolError, match="session snapshot"):
            await client.get_session()
        provider.session.snapshot.state = pb.SESSION_STATE_VERIFIED
        provider.session.snapshot.state_changed_at.seconds = 315_576_000_000
        with pytest.raises(ProviderProtocolError, match="timestamp"):
            await client.get_session()
        provider.import_result.ClearField("installed")
        with pytest.raises(ProviderProtocolError, match="typed result") as failure:
            await client.import_session(b"private-candidate", media_type="application/json", format_version="1")
        assert "private-candidate" not in str(failure.value)
        provider.verify_result.ClearField("verified")
        with pytest.raises(ProviderProtocolError, match="typed result"):
            await client.verify_candidate(b"private-candidate", media_type="application/json", format_version="1")
        provider.verify_result.rejected.reason = pb.CANDIDATE_REJECTION_REASON_UNSPECIFIED
        with pytest.raises(ProviderProtocolError, match="typed result"):
            await client.verify_candidate(b"private-candidate", media_type="application/json", format_version="1")
        provider.probe_result.result.source = pb.PROBE_SOURCE_SCHEDULED
        with pytest.raises(ProviderProtocolError, match="wrong source"):
            await client.probe(source=pb.PROBE_SOURCE_MANUAL)
        provider.probe_result.result.source = pb.PROBE_SOURCE_MANUAL
        provider.probe_result.result.ClearField("verified")
        provider.probe_result.result.failure.reason = pb.PROBE_FAILURE_REASON_UNSPECIFIED
        with pytest.raises(ProviderProtocolError, match="typed result"):
            await client.probe(source=pb.PROBE_SOURCE_MANUAL)


@pytest.mark.asyncio
async def test_owner_metadata_and_storage_error_snapshot_validation(tmp_path):
    async with served(tmp_path) as (provider, client, _, _):
        info = provider.info.provider.instances[0]
        assert (await client.get_info()).provider.instances[0].instructions[0].command == "example export"
        info.instructions[0].command = "echo one\necho two"
        assert (await client.get_info()).provider.instances[0].instructions[0].command == "echo one\necho two"
        info.accepted_session_import_formats.clear()
        with pytest.raises(ProviderProtocolError, match="candidate verification"):
            await client.get_info()

        snapshot = provider.session.snapshot
        snapshot.state = pb.SESSION_STATE_STORAGE_ERROR
        snapshot.ClearField("state_changed_at")
        assert (await client.get_session()).snapshot.state == pb.SESSION_STATE_STORAGE_ERROR
        snapshot.imported_at.CopyFrom(_now())
        snapshot.owner_presentation.message = "Stored session unavailable"
        snapshot.owner_presentation.help = "Check provider logs."
        assert (await client.get_session()).snapshot.imported_at == _now()
        snapshot.imported_at.seconds = 315_576_000_000
        with pytest.raises(ProviderProtocolError, match="timestamp"):
            await client.get_session()
        snapshot.imported_at.CopyFrom(_now())


@pytest.mark.asyncio
async def test_missing_bearer_prevents_dispatch(tmp_path):
    async with served(tmp_path) as (provider, client, token_file, _):
        token_file.unlink()
        with pytest.raises(FileNotFoundError, match="gateway-bearer"):
            await client.get_session()
        assert not provider.calls


@pytest.mark.asyncio
async def test_oversized_candidate_fails_before_dispatch(tmp_path):
    async with served(tmp_path) as (provider, client, _, _):
        with pytest.raises(ValueError, match="candidate request"):
            await client.verify_candidate(b"x" * 65_537, media_type="application/json", format_version="1")
        assert not provider.calls


@pytest.mark.asyncio
async def test_invalid_projected_bearer_and_timeout_fail_before_dispatch(tmp_path):
    async with served(tmp_path) as (provider, client, token_file, channel):
        for value in ("short", "x" * 31, "x" * 31 + "\u2603", "x" * 31 + " "):
            token_file.write_text(value)
            with pytest.raises(ValueError, match="bearer is invalid") as error:
                await client.get_info()
            assert value not in str(error.value)
        for timeout in (float("nan"), float("inf"), float("-inf"), 0):
            with pytest.raises(ValueError, match="RPC timeout must be positive"):
                ProviderControlClient(
                    channel, provider_id="example", instance_id="account", token_file=token_file, timeout_s=timeout
                )
        assert not provider.calls


def test_dial_provider_control_uses_tls_and_disables_retries(monkeypatch):
    captured: dict[str, object] = {}
    credentials = object()
    channel = object()

    def secure_channel(target, supplied_credentials, *, options):
        captured.update(target=target, credentials=supplied_credentials, options=options)
        return channel

    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: credentials)
    monkeypatch.setattr(grpc.aio, "secure_channel", secure_channel)
    assert dial_provider_control("provider.internal:443", options=(("grpc.keepalive_time_ms", 30_000),)) is channel
    assert captured == {
        "target": "provider.internal:443",
        "credentials": credentials,
        "options": (("grpc.enable_retries", 0), ("grpc.keepalive_time_ms", 30_000)),
    }
    with pytest.raises(ValueError, match="retry option cannot be overridden"):
        dial_provider_control("provider.internal:443", options=(("grpc.enable_retries", 1),))


if __name__ == "__main__":
    pytest_bazel.main()
