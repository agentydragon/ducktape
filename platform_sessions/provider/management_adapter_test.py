"""Owner HTTP routes backed by a synthetic remote provider control service."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import override

import grpc
import httpx
import pytest
import pytest_bazel
from fastapi import HTTPException, Request
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel

from platform_sessions.auth import BearerAuth
from platform_sessions.host import create_app
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.management_adapter import RemoteIntegration
from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as rpc
from platform_sessions.registry import IntegrationInstance

INSTANCE = pb.InstanceRef(provider_id="example", instance_id="account")
SESSION = {"version": 1, "provider": "example", "session": {"token": "candidate-secret"}}


class Settings(BaseModel):
    pass


def _config(instance_id: str) -> IntegrationInstance:
    return IntegrationInstance(
        instance_id, "example", True, "https://example.test", PurePosixPath(f"/data/{instance_id}"), 900, Settings()
    )


def _owner(request: Request) -> None:
    if request.cookies.get("owner") != "yes":
        raise HTTPException(status_code=401)


class Provider(rpc.SessionProviderControlServicer):
    def __init__(self) -> None:
        self.candidates: list[bytes] = []
        self.probe_calls = 0
        self.abort_import = False
        self.import_result = pb.ImportSessionResponse(instance=INSTANCE, installed=pb.SessionInstalled())
        self.verify_result = pb.VerifyCandidateResponse(instance=INSTANCE, verified=pb.CandidateVerified())
        self.snapshot = pb.SessionSnapshot(
            state=pb.SESSION_STATE_VERIFIED,
            state_changed_at=Timestamp(seconds=1_700_000_000),
            imported_at=Timestamp(seconds=1_699_999_000),
            last_probe=pb.ProbeResult(
                completed_at=Timestamp(seconds=1_700_000_000),
                source=pb.PROBE_SOURCE_SCHEDULED,
                verified=pb.ProbeVerified(),
            ),
            owner_presentation=pb.OwnerPresentation(message="Connected", help="Session verified."),
        )

    @override
    async def GetInfo(self, request, context):
        return pb.GetInfoResponse(
            provider=pb.ProviderInfo(
                provider_id="example",
                display_name="Example",
                version="test",
                instances=[
                    pb.ProviderInstanceInfo(
                        instance_id="account",
                        supports_candidate_verification=True,
                        instructions=[pb.ExportInstruction(text="Export in Chrome.", command="example export")],
                        accepted_session_import_formats=[
                            pb.SessionImportFormat(media_type="application/json", version="1")
                        ],
                    )
                ],
            )
        )

    @override
    async def GetSession(self, request, context):
        assert request.instance == INSTANCE
        return pb.GetSessionResponse(instance=INSTANCE, snapshot=self.snapshot)

    @override
    async def ImportSession(self, request, context):
        assert request.instance == INSTANCE
        self.candidates.append(request.candidate)
        if self.abort_import:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "candidate-secret")
        return self.import_result

    @override
    async def VerifyCandidate(self, request, context):
        assert request.instance == INSTANCE
        self.candidates.append(request.candidate)
        return self.verify_result

    @override
    async def Probe(self, request, context):
        assert request.instance == INSTANCE
        assert request.source == pb.PROBE_SOURCE_MANUAL
        self.probe_calls += 1
        self.snapshot.last_probe.CopyFrom(
            pb.ProbeResult(
                completed_at=Timestamp(seconds=1_700_000_100),
                source=pb.PROBE_SOURCE_MANUAL,
                failure=pb.ProbeFailure(reason=pb.PROBE_FAILURE_REASON_UPSTREAM_HTTP_ERROR),
            )
        )
        return pb.ProbeResponse(instance=INSTANCE, result=self.snapshot.last_probe)


@asynccontextmanager
async def served(tmp_path):
    provider = Provider()
    server = grpc.aio.server()
    rpc.add_SessionProviderControlServicer_to_server(provider, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    token_file = tmp_path / "control-bearer"
    token_file.write_text("a" * 32)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=(("grpc.enable_retries", 0),))
    control = ProviderControlClient(
        channel, provider_id="example", instance_id="account", token_file=token_file, timeout_s=0.5
    )
    try:
        yield provider, control, token_file, channel
    finally:
        await channel.close()
        await server.stop(grace=0)


def _http_client(*integrations: RemoteIntegration) -> httpx.AsyncClient:
    app = create_app(integrations, "https://service.test", require_owner=_owner, auth=BearerAuth([]))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://service.test")


@pytest.mark.asyncio
async def test_owner_routes_preserve_metadata_status_and_typed_candidate_outcomes(tmp_path, caplog):
    async with served(tmp_path) as (provider, control, _, _):
        integration = RemoteIntegration(_config("account"), control)
        async with _http_client(integration) as client:
            headers = {"cookie": "owner=yes", "origin": "https://service.test"}
            assert (await client.get("/api/integrations")).status_code == 401
            listed = (await client.get("/api/integrations", headers=headers)).json()
            assert listed == [
                {
                    "id": "account",
                    "type": "example",
                    "instructions": [{"text": "Export in Chrome.", "command": "example export"}],
                    "candidate_verification": True,
                }
            ]
            status = (await client.get("/api/integrations/account", headers=headers)).json()
            assert status["state"] == "ready"
            assert status["outcome"] == "success"
            assert (status["message"], status["help"]) == ("Connected", "Session verified.")
            assert status["details"][1] == {"key": "age_seconds", "value": 1000, "format": "duration"}

            provider.snapshot.ClearField("owner_presentation")
            for state, expected_message in (
                (pb.SESSION_STATE_OUTCOME_UNKNOWN, "Session outcome unknown"),
                (pb.SESSION_STATE_NO_SESSION, "No session configured"),
                (pb.SESSION_STATE_UNVERIFIED, "Session not yet verified"),
                (pb.SESSION_STATE_STORAGE_ERROR, "Could not read session state"),
            ):
                provider.snapshot.state = state
                current = (await client.get("/api/integrations/account", headers=headers)).json()
                assert current["outcome"] == "success"  # historical last probe
                assert current["message"] == expected_message  # current state is authoritative
            provider.snapshot.state = pb.SESSION_STATE_VERIFIED

            verified = await client.post("/api/integrations/account/session/verify", headers=headers, json=SESSION)
            assert verified.json() == {"outcome": "success"}
            assert provider.snapshot.last_probe.WhichOneof("result") == "verified"
            provider.verify_result = pb.VerifyCandidateResponse(
                instance=INSTANCE, unsupported=pb.CandidateVerificationUnsupported()
            )
            unsupported = await client.post("/api/integrations/account/session/verify", headers=headers, json=SESSION)
            assert (unsupported.status_code, unsupported.json()) == (404, {"error": "not_supported"})

            provider.import_result = pb.ImportSessionResponse(
                instance=INSTANCE, rejected=pb.CandidateRejected(reason=pb.CANDIDATE_REJECTION_REASON_NOT_ACCEPTED)
            )
            rejected = await client.post("/api/integrations/account/session", headers=headers, json=SESSION)
            assert (rejected.status_code, rejected.json()) == (400, {"error": "verification_failed"})
            provider.import_result = pb.ImportSessionResponse(instance=INSTANCE, installed=pb.SessionInstalled())
            imported = await client.post("/api/integrations/account/session", headers=headers, json=SESSION)
            assert imported.json() == {"configured": True}
            assert len(provider.candidates) == 4
            assert all(json.loads(candidate) == SESSION for candidate in provider.candidates)

            probed = await client.post("/api/integrations/account/verify", headers=headers)
            assert probed.status_code == 200
            assert probed.json()["outcome"] == "upstream_http_error"
            assert provider.probe_calls == 1
            assert "candidate-secret" not in caplog.text


@pytest.mark.asyncio
async def test_provider_outage_isolated_and_lost_import_is_not_retried(tmp_path, caplog):
    async with served(tmp_path) as (provider, control, token_file, _):
        bad_channel = grpc.aio.insecure_channel("127.0.0.1:1", options=(("grpc.enable_retries", 0),))
        bad = RemoteIntegration(
            _config("down"),
            ProviderControlClient(
                bad_channel, provider_id="example", instance_id="down", token_file=token_file, timeout_s=0.2
            ),
        )
        try:
            async with _http_client(RemoteIntegration(_config("account"), control), bad) as client:
                headers = {"cookie": "owner=yes", "origin": "https://service.test"}
                listed = (await client.get("/api/integrations", headers=headers)).json()
                assert [item["id"] for item in listed] == ["account", "down"]
                assert listed[1]["instructions"] == []
                assert listed[1]["candidate_verification"] is False
                assert (await client.get("/api/integrations/account", headers=headers)).json()["state"] == "ready"
                down_status = (await client.get("/api/integrations/down", headers=headers)).json()
                assert down_status["state"] == "upstream_unavailable"
                assert down_status["outcome"] == "not_checked"

                provider.abort_import = True
                response = await client.post("/api/integrations/account/session", headers=headers, json=SESSION)
                assert (response.status_code, response.json()) == (503, {"error": "outcome_unknown"})
                assert len(provider.candidates) == 1
                assert "candidate-secret" not in response.text
                assert "candidate-secret" not in caplog.text
                assert (await client.get("/api/integrations/account", headers=headers)).json()["state"] == "ready"
        finally:
            await bad_channel.close()


@pytest.mark.asyncio
async def test_invalid_envelope_never_reaches_provider(tmp_path):
    async with (
        served(tmp_path) as (provider, control, _, _),
        _http_client(RemoteIntegration(_config("account"), control)) as client,
    ):
        headers = {"cookie": "owner=yes", "origin": "https://service.test"}
        for candidate in (
            {"version": 1, "provider": "wrong", "session": {}},
            {"version": True, "provider": "example", "session": {}},
            {"version": 1, "provider": "example", "session": []},
        ):
            response = await client.post("/api/integrations/account/session", headers=headers, json=candidate)
            assert (response.status_code, response.json()) == (400, {"error": "invalid_session"})
        assert provider.candidates == []


if __name__ == "__main__":
    pytest_bazel.main()
