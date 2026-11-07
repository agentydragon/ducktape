from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import grpc
import pytest
from google.protobuf import empty_pb2

from platform_sessions.management.server import serve_management
from platform_sessions.management.service import SessionManagement
from platform_sessions.management.v1 import management_pb2 as pb, management_pb2_grpc as rpc
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.v1 import session_provider_pb2 as provider_pb


@pytest.fixture
async def management():
    ref = provider_pb.InstanceRef(provider_id="example", instance_id="account")
    client = SimpleNamespace(
        instance=ref,
        get_info=AsyncMock(
            return_value=provider_pb.GetInfoResponse(
                provider=provider_pb.ProviderInfo(
                    provider_id="example", instances=[provider_pb.ProviderInstanceInfo(instance_id="account")]
                )
            )
        ),
        get_session=AsyncMock(
            return_value=provider_pb.GetSessionResponse(
                instance=ref, snapshot=provider_pb.SessionSnapshot(state=provider_pb.SESSION_STATE_NO_SESSION)
            )
        ),
        probe=AsyncMock(return_value=provider_pb.ProbeResponse(instance=ref)),
        verify_candidate=AsyncMock(
            return_value=provider_pb.VerifyCandidateResponse(instance=ref, verified=provider_pb.CandidateVerified())
        ),
        import_session=AsyncMock(
            return_value=provider_pb.ImportSessionResponse(instance=ref, installed=provider_pb.SessionInstalled())
        ),
    )
    server = grpc.aio.server()
    rpc.add_SessionManagementServicer_to_server(
        SessionManagement({"account": cast(ProviderControlClient, client)}), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=(("grpc.enable_retries", 0),)) as channel:
        yield rpc.SessionManagementStub(channel), client
    await server.stop(None)


async def test_forwards_typed_provider_results(management):
    stub, client = management
    listing = await stub.ListInstances(empty_pb2.Empty())
    assert listing.instances[0].ref == client.instance
    assert listing.instances[0].info.instance_id == "account"
    session = await stub.GetSession(pb.InstanceRequest(instance_id="account"))
    assert session.snapshot.state == provider_pb.SESSION_STATE_NO_SESSION
    await stub.Probe(pb.InstanceRequest(instance_id="account"))
    client.probe.assert_awaited_once_with(source=provider_pb.PROBE_SOURCE_MANUAL)
    candidate = pb.CandidateRequest(
        instance_id="account", media_type="application/json", format_version="1", candidate=b"{}"
    )
    assert (await stub.VerifyCandidate(candidate)).HasField("verified")
    assert (await stub.ImportSession(candidate)).HasField("installed")
    client.import_session.assert_awaited_once_with(b"{}", media_type="application/json", format_version="1")


async def test_missing_provider_and_ambiguous_import(management):
    stub, client = management
    with pytest.raises(grpc.aio.AioRpcError) as missing:
        await stub.GetSession(pb.InstanceRequest(instance_id="missing"))
    assert missing.value.code() == grpc.StatusCode.NOT_FOUND
    client.import_session.side_effect = OSError("sensitive upstream detail")
    request = pb.CandidateRequest(
        instance_id="account", media_type="application/json", format_version="1", candidate=b"{}"
    )
    with pytest.raises(grpc.aio.AioRpcError) as failed:
        await stub.ImportSession(request)
    assert failed.value.code() == grpc.StatusCode.UNKNOWN
    assert "sensitive" not in (failed.value.details() or "")
    client.import_session.assert_awaited_once()


async def test_management_listener_requires_its_bearer(tmp_path):
    token_file = tmp_path / "management-token"
    token_file.write_text("x" * 40)
    async with serve_management({}, token_file) as target, grpc.aio.insecure_channel(target) as channel:
        stub = rpc.SessionManagementStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as failure:
            await stub.ListInstances(empty_pb2.Empty())
        assert failure.value.code() == grpc.StatusCode.UNAUTHENTICATED
        response = await stub.ListInstances(empty_pb2.Empty(), metadata=(("authorization", "Bearer " + "x" * 40),))
        assert not response.instances


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
