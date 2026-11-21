"""Control-service tests against a synthetic provider on a real loopback gRPC server."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import grpc
import pytest
import pytest_bazel
from filelock import Timeout
from google.protobuf import empty_pb2, json_format
from google.protobuf.message import Message
from python.runfiles import runfiles

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.provider import session_provider_pb2 as pb, session_provider_pb2_grpc as pb_grpc, validation
from platform_sessions.provider.instance_ownership import InstanceOwnership
from platform_sessions.provider.server import (
    ProviderBackend,
    SessionProviderControlService,
    add_session_provider_control_service,
)

_CANDIDATE = b"synthetic-session-candidate-do-not-echo"
_CANDIDATE_MEDIA_TYPE = "application/vnd.example.session+json"


class SyntheticProvider(ProviderBackend):
    def __init__(self) -> None:
        self.snapshot = pb.SessionSnapshot(known=pb.KnownSession(state=pb.SESSION_STATE_NO_SESSION))
        self.snapshot.known.state_changed_at.GetCurrentTime()
        self.imported_candidates: list[bytes] = []
        self.probes: list[pb.ProbeSource] = []
        self.probe_outcome = pb.ProbeResult(verified=empty_pb2.Empty())
        self.probe_error: Exception | None = None
        self.session_reads = 0

    @property
    def info(self) -> pb.ProviderInfo:
        return pb.ProviderInfo(
            provider_id="example",
            display_name="Example provider",
            instances=[
                pb.ProviderInstanceInfo(
                    instance_id="primary",
                    accepted_session_import_formats=[pb.SessionImportFormat(media_type=_CANDIDATE_MEDIA_TYPE)],
                    instructions=[
                        pb.ExportInstruction(text="Export a session from the provider account."),
                        pb.ExportInstruction(text="Import the exported session here.", command="copy"),
                    ],
                ),
                pb.ProviderInstanceInfo(
                    instance_id="secondary",
                    accepted_session_import_formats=[pb.SessionImportFormat(media_type=_CANDIDATE_MEDIA_TYPE)],
                ),
            ],
        )

    async def get_session(self, instance_id: str) -> pb.SessionSnapshot:
        assert instance_id in {"primary", "secondary"}
        self.session_reads += 1
        snapshot = pb.SessionSnapshot()
        snapshot.CopyFrom(self.snapshot)
        return snapshot

    async def import_session(self, instance_id: str, *, media_type: str, candidate: bytes) -> pb.ImportSessionResponse:
        assert instance_id == "primary"
        assert media_type == _CANDIDATE_MEDIA_TYPE
        self.imported_candidates.append(candidate)
        return pb.ImportSessionResponse(installed=empty_pb2.Empty())

    async def probe(self, instance_id: str, *, source: pb.ProbeSource):
        assert instance_id == "primary"
        self.probes.append(source)
        if self.probe_error is not None:
            raise self.probe_error
        if self.probe_outcome.WhichOneof("outcome") == "outcome_unknown":
            self.snapshot.known.state = pb.SESSION_STATE_OUTCOME_UNKNOWN
            self.snapshot.known.state_changed_at.GetCurrentTime()
        return self.probe_outcome


class InteractiveSyntheticProvider(SyntheticProvider):
    def __init__(self) -> None:
        super().__init__()
        self.finishes: list[str] = []

    @property
    def info(self) -> pb.ProviderInfo:
        info = super().info
        info.instances[0].interactive_sign_in.SetInParent()
        return info

    async def finish_interactive_sign_in(self, instance_id: str) -> pb.ProbeResult:
        self.finishes.append(instance_id)
        return pb.ProbeResult(verified=empty_pb2.Empty())


class BlockingImportProvider(SyntheticProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.finish = asyncio.Event()
        self.completed = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def import_session(self, instance_id: str, *, media_type: str, candidate: bytes) -> pb.ImportSessionResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.imported_candidates.append(candidate)
        if len(self.imported_candidates) == 2:
            self.second_started.set()
        self.started.set()
        await self.finish.wait()
        self.active -= 1
        self.completed.set()
        return pb.ImportSessionResponse(installed=empty_pb2.Empty())


@asynccontextmanager
async def served_provider(
    provider: ProviderBackend,
) -> AsyncIterator[tuple[pb_grpc.SessionProviderControlStub, SessionProviderControlService]]:
    server = grpc.aio.server()
    temp = TemporaryDirectory()
    ownership = {
        item.instance_id: InstanceOwnership(Path(temp.name) / item.instance_id) for item in provider.info.instances
    }
    service = add_session_provider_control_service(server, provider, ownership)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield pb_grpc.SessionProviderControlStub(channel), service
    finally:
        await channel.close()
        await server.stop(grace=0)
        assert await service.drain()
        await service.close()
        temp.cleanup()


def import_request(**kwargs) -> pb.ImportSessionRequest:
    return pb.ImportSessionRequest(**kwargs)


def instance_ref(instance_id: str = "primary") -> pb.InstanceRef:
    return pb.InstanceRef(provider_id="example", instance_id=instance_id)


async def test_info_advertises_import_formats_and_instructions() -> None:
    async with served_provider(SyntheticProvider()) as (client, _service):
        info = await client.GetInfo(pb.GetInfoRequest())
        assert len(info.provider.instances) == 2
        assert info.provider.instances[0].accepted_session_import_formats[0].media_type == _CANDIDATE_MEDIA_TYPE
        assert info.provider.instances[0].instructions[0].text == "Export a session from the provider account."
        assert not info.provider.instances[0].instructions[0].HasField("command")
        assert info.provider.instances[0].instructions[1].command == "copy"


async def test_interactive_finish_is_instance_scoped_and_returns_verified_probe() -> None:
    provider = InteractiveSyntheticProvider()
    async with served_provider(provider) as (client, _service):
        info = await client.GetInfo(pb.GetInfoRequest())
        assert info.provider.instances[0].HasField("interactive_sign_in")
        assert not info.provider.instances[1].HasField("interactive_sign_in")

        finished = await client.FinishInteractiveSignIn(pb.FinishInteractiveSignInRequest(instance=instance_ref()))
        assert finished.instance == instance_ref()
        assert finished.result.WhichOneof("outcome") == "verified"
        assert finished.result.source == pb.PROBE_SOURCE_VERIFY
        assert finished.result.HasField("completed_at")
        assert provider.finishes == ["primary"]

        with pytest.raises(grpc.aio.AioRpcError) as unsupported:
            await client.FinishInteractiveSignIn(pb.FinishInteractiveSignInRequest(instance=instance_ref("secondary")))
        assert unsupported.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert provider.finishes == ["primary"]


async def test_import_probe_results_are_typed_and_candidate_is_not_echoed() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(
        outcome_unknown=pb.OperationOutcomeUnknown(credential_rotation_may_have_occurred=True)
    )
    async with served_provider(provider) as (client, _service):
        imported = await client.ImportSession(
            import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
        )
        assert imported.WhichOneof("outcome") == "installed"
        assert json_format.MessageToDict(imported)["installed"] == {}
        assert provider.imported_candidates == [_CANDIDATE]
        assert _CANDIDATE not in imported.SerializeToString()

        result = await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_SCHEDULED))
        assert result.result.WhichOneof("outcome") == "outcome_unknown"
        assert result.result.outcome_unknown.credential_rotation_may_have_occurred
        assert result.result.source == pb.PROBE_SOURCE_SCHEDULED
        assert len(provider.probes) == 1


async def test_probe_preserves_provider_completion_time_and_validates_it() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome.completed_at.seconds = 1_700_000_000
    async with served_provider(provider) as (client, _service):
        request = pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL)
        result = await client.Probe(request)
        assert result.result.completed_at.seconds == 1_700_000_000

        provider.probe_outcome.ClearField("completed_at")
        result = await client.Probe(request)
        assert result.result.HasField("completed_at")
        assert result.result.completed_at.seconds > 1_700_000_000

        provider.probe_outcome.completed_at.seconds = 253_402_300_800
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(request)
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_import_failure_preserves_bounded_diagnostics_and_rejects_invalid_status() -> None:
    class Provider(SyntheticProvider):
        outcome = pb.ImportSessionResponse(
            failure=pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR, upstream_http_status=429)
        )

        async def import_session(
            self, instance_id: str, *, media_type: str, candidate: bytes
        ) -> pb.ImportSessionResponse:
            return self.outcome

    provider = Provider()
    request = import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
    async with served_provider(provider) as (client, _):
        result = await client.ImportSession(request)
        assert result.WhichOneof("outcome") == "failure"
        assert result.failure.upstream_http_status == 429
        provider.outcome = pb.ImportSessionResponse(
            outcome_unknown=pb.OperationOutcomeUnknown(
                credential_rotation_may_have_occurred=True, reason=pb.UNKNOWN_OUTCOME_REASON_TIMEOUT
            )
        )
        result = await client.ImportSession(request)
        assert result.WhichOneof("outcome") == "outcome_unknown"
        assert result.outcome_unknown.reason == pb.UNKNOWN_OUTCOME_REASON_TIMEOUT
        provider.outcome = pb.ImportSessionResponse(
            outcome_unknown=pb.OperationOutcomeUnknown(
                credential_rotation_may_have_occurred=True,
                reason=pb.UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR,
                upstream_http_status=429,
            )
        )
        result = await client.ImportSession(request)
        assert result.WhichOneof("outcome") == "outcome_unknown"
        assert result.outcome_unknown.upstream_http_status == 429
        provider.outcome.outcome_unknown.upstream_http_status = 999
        response = await client.ImportSession(request)
        assert response.WhichOneof("outcome") == "outcome_unknown"
        provider.outcome = pb.ImportSessionResponse(
            failure=pb.ImportFailure(reason=pb.IMPORT_FAILURE_REASON_INVALID_RESPONSE, upstream_http_status=429)
        )
        response = await client.ImportSession(request)
        assert response.WhichOneof("outcome") == "outcome_unknown"


async def test_backend_result_requires_outcome_and_cannot_spoof_instance() -> None:
    class Provider(SyntheticProvider):
        result = pb.ImportSessionResponse()

        async def import_session(
            self, instance_id: str, *, media_type: str, candidate: bytes
        ) -> pb.ImportSessionResponse:
            return self.result

    provider = Provider()
    request = import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
    async with served_provider(provider) as (client, _):
        response = await client.ImportSession(request)
        assert response.WhichOneof("outcome") == "outcome_unknown"
        provider.result = pb.ImportSessionResponse(
            instance=pb.InstanceRef(provider_id="wrong", instance_id="wrong"), installed=empty_pb2.Empty()
        )
        provider.result.MergeFromString(b"\xa2\x06" + bytes([len(_CANDIDATE)]) + _CANDIDATE)
        response = await client.ImportSession(request)
        assert _CANDIDATE not in response.SerializeToString()
        assert response.instance == request.instance
        assert response.WhichOneof("outcome") == "installed"
        provider.probe_outcome = pb.ProbeResult()
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert error.value.code() == grpc.StatusCode.INTERNAL


def test_unknown_future_outcome_field_is_not_treated_as_success() -> None:
    # Field 100 represents a future oneof variant unknown to this schema.
    result = pb.ImportSessionResponse.FromString(b"\xa2\x06\x00")
    assert result.WhichOneof("outcome") is None
    with pytest.raises(ValueError, match="has no outcome"):
        validation.import_result(result)


def _protocol_examples() -> dict[str, list[dict[str, Any]]]:
    files = runfiles.Create()
    assert files is not None
    path = files.Rlocation("ducktape/platform_sessions/testdata/protocol_validation_examples.json")
    assert path is not None
    return cast(dict[str, list[dict[str, Any]]], json.loads(Path(path).read_text()))


def _assert_protocol_examples(key: str, message_type: type[Message], validate: Callable[[Any], None]) -> None:
    for example in _protocol_examples()[key]:
        message = json_format.Parse(json.dumps(example["message"]), message_type())
        try:
            validate(message)
        except validation.ProviderProtocolError:
            accepted = False
        else:
            accepted = True
        assert accepted is example["valid"], example["name"]


def test_shared_snapshot_protocol_examples() -> None:
    _assert_protocol_examples("snapshots", pb.SessionSnapshot, validation.snapshot)


def test_shared_probe_result_protocol_examples() -> None:
    _assert_protocol_examples("probe_results", pb.ProbeResult, validation.probe_result)


def test_shared_import_result_protocol_examples() -> None:
    _assert_protocol_examples("import_results", pb.ImportSessionResponse, validation.import_result)


async def test_probe_failures_are_typed_and_not_collapsed_to_no_session() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(failure=pb.ProbeFailure(reason=pb.PROBE_FAILURE_REASON_STORAGE_ERROR))
    async with served_provider(provider) as (client, _service):
        result = await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert result.result.WhichOneof("outcome") == "failure"
        assert result.result.failure.reason == pb.PROBE_FAILURE_REASON_STORAGE_ERROR
        assert result.result.source == pb.PROBE_SOURCE_MANUAL
        assert provider.probes == [pb.PROBE_SOURCE_MANUAL]


@pytest.mark.parametrize("source", [pb.PROBE_SOURCE_VERIFY, pb.PROBE_SOURCE_STARTUP, pb.PROBE_SOURCE_OTHER])
async def test_internal_probe_sources_cannot_be_requested(source: pb.ProbeSource) -> None:
    provider = SyntheticProvider()
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=source))
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert provider.probes == []


async def test_probe_failure_unspecified_reason_is_a_provider_protocol_error() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(failure=pb.ProbeFailure(reason=pb.PROBE_FAILURE_REASON_UNSPECIFIED))
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert error.value.code() == grpc.StatusCode.INTERNAL
        assert error.value.details() == "provider returned an invalid operation result"


async def test_probe_without_an_installed_session_has_its_own_result() -> None:
    provider = SyntheticProvider()
    provider.probe_outcome = pb.ProbeResult(no_session=empty_pb2.Empty())
    async with served_provider(provider) as (client, _service):
        result = await client.Probe(pb.ProbeRequest(instance=instance_ref(), source=pb.PROBE_SOURCE_MANUAL))
        assert result.result.WhichOneof("outcome") == "no_session"
        assert provider.snapshot.known.state == pb.SESSION_STATE_NO_SESSION


async def test_unsupported_format_and_invalid_request_do_not_reach_provider() -> None:
    provider = SyntheticProvider()
    async with served_provider(provider) as (client, _service):
        rejected = await client.ImportSession(
            import_request(instance=instance_ref(), media_type="application/x-unsupported", candidate=_CANDIDATE)
        )
        assert rejected.WhichOneof("outcome") == "rejected"
        assert rejected.rejected.reason == pb.CANDIDATE_REJECTION_REASON_UNSUPPORTED_FORMAT
        assert provider.imported_candidates == []

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(
                import_request(instance=instance_ref("missing"), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
            )
        assert error.value.code() == grpc.StatusCode.NOT_FOUND
        details = error.value.details() or ""
        assert _CANDIDATE.decode() not in details
        assert provider.imported_candidates == []

        oversized = b"x" * (MAX_SESSION_BYTES + 1)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.ImportSession(
                import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=oversized)
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        details = error.value.details() or ""
        assert "x" * 100 not in details
        assert provider.imported_candidates == []


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
        assert provider.probes == [pb.PROBE_SOURCE_MANUAL]


async def test_invalid_provider_snapshot_is_not_sent_as_success() -> None:
    provider = SyntheticProvider()
    provider.snapshot = pb.SessionSnapshot(known=pb.KnownSession(state=pb.SESSION_STATE_UNVERIFIED))
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL
        provider.snapshot = pb.SessionSnapshot()
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_invalid_state_timestamp_is_not_sent_as_success() -> None:
    provider = SyntheticProvider()
    provider.snapshot.known.state_changed_at.seconds = 253402300800
    async with served_provider(provider) as (client, _service):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_snapshot_supports_owner_presentation_imported_at_and_storage_error() -> None:
    provider = SyntheticProvider()
    provider.snapshot = pb.SessionSnapshot(
        known=pb.KnownSession(state=pb.SESSION_STATE_VERIFIED),
        owner_presentation=pb.OwnerPresentation(
            message="Session is verified.", help="If this changes, export a fresh session from the provider."
        ),
    )
    provider.snapshot.known.state_changed_at.GetCurrentTime()
    provider.snapshot.imported_at.GetCurrentTime()
    async with served_provider(provider) as (client, _service):
        response = await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert response.snapshot.known.state == pb.SESSION_STATE_VERIFIED
        assert response.snapshot.owner_presentation.message == "Session is verified."
        assert response.snapshot.owner_presentation.help.startswith("If this changes")
        assert response.snapshot.HasField("imported_at")

        provider.snapshot = pb.SessionSnapshot(storage_error=pb.SessionStorageError())
        storage_error = await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert storage_error.snapshot.WhichOneof("status") == "storage_error"
        assert not storage_error.snapshot.storage_error.HasField("state_changed_at")

        provider.snapshot.storage_error.state_changed_at.seconds = 253402300800
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL

        provider.snapshot = pb.SessionSnapshot(
            known=pb.KnownSession(state=pb.SESSION_STATE_VERIFIED),
            owner_presentation=pb.OwnerPresentation(message="Verified."),
        )
        provider.snapshot.known.state_changed_at.GetCurrentTime()
        provider.snapshot.imported_at.seconds = 253402300800
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await client.GetSession(pb.GetSessionRequest(instance=instance_ref()))
        assert error.value.code() == grpc.StatusCode.INTERNAL


async def test_disconnected_import_finishes_and_serializes_new_submissions() -> None:
    provider = BlockingImportProvider()
    request = import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
    async with served_provider(provider) as (client, service):
        call = client.ImportSession(request)
        await asyncio.wait_for(provider.started.wait(), 5)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not await service.drain(drain_seconds=0)
        provider.finish.set()
        await asyncio.wait_for(provider.completed.wait(), 5)
        assert await service.drain()
        assert provider.imported_candidates == [_CANDIDATE]


async def test_explicit_reimport_can_submit_identical_bytes_and_is_serialized() -> None:
    provider = BlockingImportProvider()
    request = import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
    async with served_provider(provider) as (client, _service):
        first = client.ImportSession(request)
        await asyncio.wait_for(provider.started.wait(), 5)
        second = client.ImportSession(request)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(provider.second_started.wait(), 0.1)
        finally:
            provider.finish.set()
        results = await asyncio.gather(first, second)
        assert all(result.WhichOneof("outcome") == "installed" for result in results)
        assert provider.imported_candidates == [_CANDIDATE, _CANDIDATE]
        assert provider.max_active == 1


async def test_drain_retains_instance_ownership_until_owner_is_stopped(tmp_path: Path) -> None:
    provider = BlockingImportProvider()
    ownership = {item.instance_id: InstanceOwnership(tmp_path / item.instance_id) for item in provider.info.instances}
    service = SessionProviderControlService(provider, ownership)
    server = grpc.aio.server()
    pb_grpc.add_SessionProviderControlServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    client = pb_grpc.SessionProviderControlStub(channel)
    try:
        request = import_request(instance=instance_ref(), media_type=_CANDIDATE_MEDIA_TYPE, candidate=_CANDIDATE)
        call = client.ImportSession(request)
        await asyncio.wait_for(provider.started.wait(), 5)
        with pytest.raises(RuntimeError, match="operations must be drained"):
            await service.close()
        assert not await service.drain(drain_seconds=0.01)
        with pytest.raises(RuntimeError, match="operations must be drained"):
            await service.close()
        with pytest.raises(Timeout):
            InstanceOwnership(tmp_path / "primary")
        provider.finish.set()
        assert (await call).WhichOneof("outcome") == "installed"
        assert await service.drain()
        assert await service.drain()
        with pytest.raises(Timeout):
            InstanceOwnership(tmp_path / "primary")
        await service.close()
        await service.close()
        successor = InstanceOwnership(tmp_path / "primary")
        successor.close()
    finally:
        provider.finish.set()
        await channel.close()
        await server.stop(grace=0)
        await service.drain()
        await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
