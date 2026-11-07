"""Control-service tests against a synthetic provider on a real loopback gRPC server."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest
import pytest_bazel

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as pb_grpc
from platform_sessions.provider.v1.server import (
    ProbeSource,
    ProviderBackend,
    SessionProviderControlService,
    add_session_provider_control_service,
)

_CANDIDATE = b"synthetic-session-candidate-do-not-echo"
_CANDIDATE_MEDIA_TYPE = "application/vnd.example.session+json"
_CANDIDATE_VERSION = "1"


class SyntheticProvider(ProviderBackend):
    def __init__(self) -> None:
        self.snapshot = pb.SessionSnapshot(state=pb.SESSION_STATE_NO_SESSION)
        self.snapshot.state_changed_at.GetCurrentTime()
        self.imported_candidates: list[bytes] = []
        self.probes: list[ProbeSource] = []
        self.verified_candidates: list[bytes] = []
        self.probe_outcome = pb.ProbeResult(outcome=pb.PROBE_OUTCOME_VERIFIED)
        self.candidate_verification_outcome = pb.VerifyCandidateResponse(outcome=pb.VERIFY_OUTCOME_VERIFIED)
        self.probe_error: Exception | None = None
        self.session_reads = 0

    @property
    def info(self) -> pb.ProviderInfo:
        return pb.ProviderInfo(
            provider_id="example",
            display_name="Example provider",
            version="test-build",
            instances=[
                pb.ProviderInstanceInfo(
                    instance_id="primary",
                    accepted_session_import_formats=[
                        pb.SessionImportFormat(media_type=_CANDIDATE_MEDIA_TYPE, version=_CANDIDATE_VERSION)
                    ],
                    supports_candidate_verification=True,
                    instructions=[
                        pb.ExportInstruction(text="Export a session from the provider account."),
                        pb.ExportInstruction(text="Import the exported session here.", command="copy"),
                    ],
                ),
                pb.ProviderInstanceInfo(
                    instance_id="without-verification",
                    accepted_session_import_formats=[
                        pb.SessionImportFormat(media_type=_CANDIDATE_MEDIA_TYPE, version=_CANDIDATE_VERSION)
                    ],
                ),
            ],
        )

    async def get_session(self, instance_id: str) -> pb.SessionSnapshot:
        assert instance_id in {"primary", "without-verification"}
        self.session_reads += 1
        snapshot = pb.SessionSnapshot()
        snapshot.CopyFrom(self.snapshot)
        return snapshot

    async def import_session(
        self, instance_id: str, *, media_type: str, format_version: str, candidate: bytes
    ) -> pb.ImportSessionResponse:
        assert instance_id == "primary"
        assert (media_type, format_version) == (_CANDIDATE_MEDIA_TYPE, _CANDIDATE_VERSION)
        self.imported_candidates.append(candidate)
        return pb.ImportSessionResponse(outcome=pb.IMPORT_OUTCOME_INSTALLED)

    async def probe(self, instance_id: str, *, source: ProbeSource):
        assert instance_id == "primary"
        self.probes.append(source)
        if self.probe_error is not None:
            raise self.probe_error
        if self.probe_outcome.outcome == pb.PROBE_OUTCOME_UNKNOWN:
            self.snapshot.state = pb.SESSION_STATE_OUTCOME_UNKNOWN
            self.snapshot.state_changed_at.GetCurrentTime()
        return self.probe_outcome

    async def verify_candidate(
        self, instance_id: str, *, media_type: str, format_version: str, candidate: bytes
    ) -> pb.VerifyCandidateResponse:
        assert instance_id == "primary"
        assert (media_type, format_version) == (_CANDIDATE_MEDIA_TYPE, _CANDIDATE_VERSION)
        self.verified_candidates.append(candidate)
        return self.candidate_verification_outcome


@asynccontextmanager
async def served_provider(
    provider: ProviderBackend,
) -> AsyncIterator[tuple[pb_grpc.SessionProviderControlStub, SessionProviderControlService]]:
    server = grpc.aio.server()
    service = add_session_provider_control_service(server, provider)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield pb_grpc.SessionProviderControlStub(channel), service
    finally:
        await channel.close()
        await server.stop(grace=0)


def instance_ref(instance_id: str = "primary") -> pb.InstanceRef:
    return pb.InstanceRef(provider_id="example", instance_id=instance_id)


async def test_info_candidate_verification_is_separate_from_installed_session_probe_state() -> None:
    provider = SyntheticProvider()
    async with served_provider(provider) as (client, _service):
        info = await client.GetInfo(pb.GetInfoRequest())
        assert len(info.provider.instances) == 2
        assert info.provider.instances[0].supports_candidate_verification
        assert info.provider.instances[0].instructions[0].text == "Export a session from the provider account."
        assert not info.provider.instances[0].instructions[0].HasField("command")
        assert info.provider.instances[0].instructions[1].command == "copy"

        verified = await client.VerifyCandidate(
            pb.VerifyCandidateRequest(
                instance=instance_ref(),
                media_type=_CANDIDATE_MEDIA_TYPE,
                format_version=_CANDIDATE_VERSION,
                candidate=_CANDIDATE,
            )
        )
        assert verified.outcome == pb.VERIFY_OUTCOME_VERIFIED
        assert provider.verified_candidates == [_CANDIDATE]
        assert provider.probes == []

        snapshot = await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert snapshot.snapshot.state == pb.SESSION_STATE_NO_SESSION
        assert not snapshot.snapshot.HasField("last_probe")

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.VerifyCandidate(
                pb.VerifyCandidateRequest(
                    instance=instance_ref("without-verification"),
                    media_type=_CANDIDATE_MEDIA_TYPE,
                    format_version=_CANDIDATE_VERSION,
                    candidate=_CANDIDATE,
                )
            )
        assert error.value.code() == grpc.StatusCode.UNIMPLEMENTED
        assert provider.verified_candidates == [_CANDIDATE]


async def test_advertised_candidate_verification_requires_an_implementation() -> None:
    class MissingVerification(SyntheticProvider):
        verify_candidate = ProviderBackend.verify_candidate

    async with served_provider(MissingVerification()) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.VerifyCandidate(
                pb.VerifyCandidateRequest(
                    instance=instance_ref(),
                    media_type=_CANDIDATE_MEDIA_TYPE,
                    format_version=_CANDIDATE_VERSION,
                    candidate=_CANDIDATE,
                )
            )
        assert error.value.code() == grpc.StatusCode.INTERNAL
        assert _CANDIDATE.decode() not in (error.value.details() or "")


async def test_ambiguous_candidate_verification_does_not_change_installed_snapshot() -> None:
    provider = SyntheticProvider()
    provider.candidate_verification_outcome = pb.VerifyCandidateResponse(
        outcome=pb.VERIFY_OUTCOME_UNKNOWN,
        outcome_unknown=pb.OperationOutcomeUnknown(credential_rotation_may_have_occurred=True),
    )
    async with served_provider(provider) as (client, _service):
        result = await client.VerifyCandidate(
            pb.VerifyCandidateRequest(
                instance=instance_ref(),
                media_type=_CANDIDATE_MEDIA_TYPE,
                format_version=_CANDIDATE_VERSION,
                candidate=_CANDIDATE,
            )
        )
        assert result.outcome == pb.VERIFY_OUTCOME_UNKNOWN
        assert result.outcome_unknown.credential_rotation_may_have_occurred
        snapshot = await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert snapshot.snapshot.state == pb.SESSION_STATE_NO_SESSION
        assert not snapshot.snapshot.HasField("last_probe")
        assert provider.probes == []


async def test_import_probe_results_are_typed_and_candidate_is_not_echoed() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(
        outcome=pb.PROBE_OUTCOME_UNKNOWN,
        outcome_unknown=pb.OperationOutcomeUnknown(credential_rotation_may_have_occurred=True),
    )
    async with served_provider(provider) as (client, _service):
        imported = await client.ImportSession(
            pb.ImportSessionRequest(
                instance=instance_ref(),
                media_type=_CANDIDATE_MEDIA_TYPE,
                format_version=_CANDIDATE_VERSION,
                candidate=_CANDIDATE,
            )
        )
        assert imported.outcome == pb.IMPORT_OUTCOME_INSTALLED
        assert provider.imported_candidates == [_CANDIDATE]
        assert _CANDIDATE not in imported.SerializeToString()

        result = await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_SCHEDULED))
        assert result.result.outcome == pb.PROBE_OUTCOME_UNKNOWN
        assert result.result.outcome_unknown.credential_rotation_may_have_occurred
        assert result.result.source == pb.PROBE_SOURCE_SCHEDULED
        assert len(provider.probes) == 1


async def test_import_failure_preserves_bounded_diagnostics_and_rejects_invalid_status() -> None:
    class Provider(SyntheticProvider):
        outcome = pb.ImportSessionResponse(
            outcome=pb.IMPORT_OUTCOME_FAILURE,
            failure=pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR, upstream_http_status=429),
        )

        async def import_session(
            self, instance_id: str, *, media_type: str, format_version: str, candidate: bytes
        ) -> pb.ImportSessionResponse:
            return self.outcome

    provider = Provider()
    request = pb.ImportSessionRequest(
        instance=instance_ref(),
        media_type=_CANDIDATE_MEDIA_TYPE,
        format_version=_CANDIDATE_VERSION,
        candidate=_CANDIDATE,
    )
    async with served_provider(provider) as (client, _):
        result = await client.ImportSession(request)
        assert result.outcome == pb.IMPORT_OUTCOME_FAILURE
        assert result.failure.upstream_http_status == 429
        provider.outcome = pb.ImportSessionResponse(
            outcome=pb.IMPORT_OUTCOME_UNKNOWN,
            outcome_unknown=pb.OperationOutcomeUnknown(
                credential_rotation_may_have_occurred=True, reason=pb.UNKNOWN_OUTCOME_REASON_TIMEOUT
            ),
        )
        result = await client.ImportSession(request)
        assert result.outcome == pb.IMPORT_OUTCOME_UNKNOWN
        assert result.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_TIMEOUT
        provider.outcome = pb.ImportSessionResponse(
            outcome=pb.IMPORT_OUTCOME_UNKNOWN,
            outcome_unknown=pb.OperationOutcomeUnknown(
                credential_rotation_may_have_occurred=True,
                reason=pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR,
                upstream_http_status=429,
            ),
        )
        result = await client.ImportSession(request)
        assert result.outcome == pb.IMPORT_OUTCOME_UNKNOWN
        assert result.outcome_unknown.upstream_http_status == 429
        provider.outcome.outcome_unknown.upstream_http_status = 999
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(request)
        assert error.value.code() == grpc.StatusCode.INTERNAL
        provider.outcome = pb.ImportSessionResponse(
            outcome=pb.IMPORT_OUTCOME_FAILURE,
            failure=pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_INVALID_RESPONSE, upstream_http_status=429),
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(request)
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_backend_result_requires_outcome_and_cannot_spoof_instance() -> None:
    class Provider(SyntheticProvider):
        result = pb.ImportSessionResponse()

        async def import_session(
            self, instance_id: str, *, media_type: str, format_version: str, candidate: bytes
        ) -> pb.ImportSessionResponse:
            return self.result

    provider = Provider()
    request = pb.ImportSessionRequest(
        instance=instance_ref(),
        media_type=_CANDIDATE_MEDIA_TYPE,
        format_version=_CANDIDATE_VERSION,
        candidate=_CANDIDATE,
    )
    async with served_provider(provider) as (client, _):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(request)
        assert error.value.code() == grpc.StatusCode.INTERNAL
        provider.result = pb.ImportSessionResponse(
            instance=pb.InstanceRef(provider_id="wrong", instance_id="wrong"), outcome=pb.IMPORT_OUTCOME_INSTALLED
        )
        response = await client.ImportSession(request)
        assert response.instance == request.instance
        assert response.outcome == pb.IMPORT_OUTCOME_INSTALLED
        provider.probe_outcome = pb.ProbeResult()
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_probe_failures_are_typed_and_not_collapsed_to_no_session() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(
        outcome=pb.PROBE_OUTCOME_FAILURE, failure=pb.ProbeFailure(reason=pb.PROBE_FAILURE_REASON_STORAGE_ERROR)
    )
    async with served_provider(provider) as (client, _service):
        result = await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert result.result.outcome == pb.PROBE_OUTCOME_FAILURE
        assert result.result.failure.reason == pb.PROBE_FAILURE_REASON_STORAGE_ERROR
        assert result.result.source == pb.PROBE_SOURCE_MANUAL
        assert provider.probes == [ProbeSource.MANUAL]


async def test_probe_failure_unspecified_reason_is_a_provider_protocol_error() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(
        outcome=pb.PROBE_OUTCOME_FAILURE, failure=pb.ProbeFailure(reason=pb.PROBE_FAILURE_REASON_UNSPECIFIED)
    )
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert error.value.code() == grpc.StatusCode.INTERNAL
        assert error.value.details() == "provider returned an invalid operation result"


async def test_probe_without_an_installed_session_has_its_own_result() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(outcome=pb.PROBE_OUTCOME_NO_SESSION)
    async with served_provider(provider) as (client, _service):
        result = await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert result.result.outcome == pb.PROBE_OUTCOME_NO_SESSION
        assert provider.snapshot.state == pb.SESSION_STATE_NO_SESSION


async def test_unsupported_format_and_invalid_request_do_not_reach_provider() -> None:
    provider = SyntheticProvider()
    async with served_provider(provider) as (client, _service):
        rejected = await client.ImportSession(
            pb.ImportSessionRequest(
                instance=instance_ref(),
                media_type="application/x-unsupported",
                format_version="99",
                candidate=_CANDIDATE,
            )
        )
        assert rejected.outcome == pb.IMPORT_OUTCOME_REJECTED
        assert rejected.rejected.reason == pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT
        assert provider.imported_candidates == []

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(
                pb.ImportSessionRequest(
                    instance=instance_ref("missing"),
                    media_type=_CANDIDATE_MEDIA_TYPE,
                    format_version=_CANDIDATE_VERSION,
                    candidate=_CANDIDATE,
                )
            )
        assert error.value.code() == grpc.StatusCode.NOT_FOUND
        details = error.value.details() or ""
        assert _CANDIDATE.decode() not in details
        assert provider.imported_candidates == []

        oversized = b"x" * (MAX_SESSION_BYTES + 1)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(
                pb.ImportSessionRequest(
                    instance=instance_ref(),
                    media_type=_CANDIDATE_MEDIA_TYPE,
                    format_version=_CANDIDATE_VERSION,
                    candidate=oversized,
                )
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        details = error.value.details() or ""
        assert "x" * 100 not in details
        assert provider.imported_candidates == []

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.VerifyCandidate(
                pb.VerifyCandidateRequest(
                    instance=instance_ref(),
                    media_type=_CANDIDATE_MEDIA_TYPE,
                    format_version=_CANDIDATE_VERSION,
                    candidate=oversized,
                )
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        details = error.value.details() or ""
        assert "x" * 100 not in details
        assert provider.verified_candidates == []


async def test_backend_exception_is_sanitized_and_never_retried(caplog: pytest.LogCaptureFixture) -> None:
    provider = SyntheticProvider()
    provider.probe_error = RuntimeError("private session cookie: synthetic-secret")
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert error.value.code() == grpc.StatusCode.INTERNAL
        details = error.value.details() or ""
        assert details == "provider operation failed"
        assert "synthetic-secret" not in details
        assert "synthetic-secret" not in caplog.text
        assert provider.probes == [ProbeSource.MANUAL]


async def test_invalid_provider_snapshot_is_not_sent_as_success() -> None:
    provider = SyntheticProvider()
    provider.snapshot = pb.SessionSnapshot(state=pb.SESSION_STATE_UNVERIFIED)
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_invalid_state_timestamp_is_not_sent_as_success() -> None:
    provider = SyntheticProvider()
    provider.snapshot.state_changed_at.seconds = 253402300800
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_snapshot_supports_owner_presentation_imported_at_and_storage_error() -> None:
    provider = SyntheticProvider()
    provider.snapshot = pb.SessionSnapshot(
        state=pb.SESSION_STATE_VERIFIED,
        owner_presentation=pb.OwnerPresentation(
            message="Session is verified.", help="If this changes, export a fresh session from the provider."
        ),
    )
    provider.snapshot.state_changed_at.GetCurrentTime()
    provider.snapshot.imported_at.GetCurrentTime()
    async with served_provider(provider) as (client, _service):
        response = await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert response.snapshot.state == pb.SESSION_STATE_VERIFIED
        assert response.snapshot.owner_presentation.message == "Session is verified."
        assert response.snapshot.owner_presentation.help.startswith("If this changes")
        assert response.snapshot.HasField("imported_at")

        provider.snapshot = pb.SessionSnapshot(state=pb.SESSION_STATE_STORAGE_ERROR)
        storage_error = await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert storage_error.snapshot.state == pb.SESSION_STATE_STORAGE_ERROR
        assert not storage_error.snapshot.HasField("state_changed_at")

        provider.snapshot = pb.SessionSnapshot(
            state=pb.SESSION_STATE_VERIFIED, owner_presentation=pb.OwnerPresentation(message="Verified.")
        )
        provider.snapshot.state_changed_at.GetCurrentTime()
        provider.snapshot.imported_at.seconds = 253402300800
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL


if __name__ == "__main__":
    pytest_bazel.main()
