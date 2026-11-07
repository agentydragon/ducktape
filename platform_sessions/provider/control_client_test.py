"""The gateway control client talks to a real in-process gRPC server."""

from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from typing import override

import grpc
import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from platform_sessions.provider.control_client import ProviderControlClient, ProviderProtocolError
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
                instances=[
                    pb.ProviderInstanceInfo(
                        instance_id="account",
                        routes=[
                            pb.RouteDescription(
                                operation_id="read_account",
                                method="GET",
                                path_template="/account",
                                access_class=pb.ROUTE_ACCESS_CLASS_READ_ONLY,
                            )
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
        self.probe_result = pb.ProbeResponse(
            instance=INSTANCE,
            result=pb.ProbeResult(completed_at=_now(), source=pb.PROBE_SOURCE_MANUAL, verified=pb.ProbeVerified()),
        )
        self.abort_import = False

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


@asynccontextmanager
async def served(tmp_path):
    provider = Provider()
    server = grpc.aio.server()
    rpc.add_SessionProviderControlServicer_to_server(provider, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    token_file = tmp_path / "gateway-bearer"
    token_file.write_text("first-token\n")
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
        token_file.write_text("second-token\n")
        assert (await client.get_session()).snapshot.state == pb.SESSION_STATE_VERIFIED
        candidate = b'{"version":1,"session":{"token":"secret"}}'
        assert (await client.import_session(candidate, media_type="application/json", format_version="1")).WhichOneof(
            "result"
        ) == "installed"
        assert (await client.probe(source=pb.PROBE_SOURCE_MANUAL)).result.WhichOneof("result") == "verified"
        assert provider.candidates == [candidate]
        assert provider.tokens == ["Bearer first-token", *("Bearer second-token",) * 3]
        assert provider.calls == Counter(GetInfo=1, GetSession=1, ImportSession=1, Probe=1)
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


@pytest.mark.asyncio
async def test_protocol_errors_reject_missing_and_mismatched_responses_without_candidate_data(tmp_path):
    async with served(tmp_path) as (provider, client, _, _):
        provider.info.provider.provider_id = "other"
        with pytest.raises(ProviderProtocolError, match="provider identity"):
            await client.get_info()
        provider.info.provider.provider_id = "example"
        provider.info.provider.instances[0].routes[0].access_class = pb.ROUTE_ACCESS_CLASS_UNSPECIFIED
        with pytest.raises(ProviderProtocolError, match="route declaration"):
            await client.get_info()
        provider.info.provider.instances[0].routes[0].access_class = pb.ROUTE_ACCESS_CLASS_READ_ONLY
        provider.info.provider.instances[0].routes[0].path_template = "/account/%2Fsecret"
        with pytest.raises(ProviderProtocolError, match="route declaration"):
            await client.get_info()
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
        provider.probe_result.result.source = pb.PROBE_SOURCE_SCHEDULED
        with pytest.raises(ProviderProtocolError, match="wrong source"):
            await client.probe(source=pb.PROBE_SOURCE_MANUAL)


@pytest.mark.asyncio
async def test_missing_bearer_prevents_dispatch(tmp_path):
    async with served(tmp_path) as (provider, client, token_file, _):
        token_file.unlink()
        with pytest.raises(FileNotFoundError, match="gateway-bearer"):
            await client.get_session()
        assert not provider.calls


if __name__ == "__main__":
    pytest_bazel.main()
