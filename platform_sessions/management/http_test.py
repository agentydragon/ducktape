import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI, HTTPException, Request
from python.runfiles import Runfiles

from platform_sessions.auth import BearerAuth
from platform_sessions.management.http import create_router, run_facade
from platform_sessions.management.server import serve_management
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.v1 import session_provider_pb2 as pb


def require_owner(request: Request):
    if request.headers.get("x-owner") != "yes":
        raise HTTPException(status_code=401)


async def test_owner_requests_traverse_generated_facade_once(tmp_path):
    ref = pb.InstanceRef(provider_id="example", instance_id="account")
    client = SimpleNamespace(
        instance=ref,
        get_info=AsyncMock(
            return_value=pb.GetInfoResponse(
                provider=pb.ProviderInfo(
                    provider_id="example", instances=[pb.ProviderInstanceInfo(instance_id="account")]
                )
            )
        ),
        import_session=AsyncMock(return_value=pb.ImportSessionResponse(instance=ref, installed=pb.SessionInstalled())),
    )
    token_file = tmp_path / "token"
    token_file.write_text("x" * 40)
    binary_path = Runfiles.CreateOrRaise().Rlocation(os.environ["MANAGEMENT_FACADE_RLOCATION"])
    assert binary_path is not None
    binary = Path(binary_path)
    async with (
        serve_management({"account": cast(ProviderControlClient, client)}, token_file) as target,
        run_facade(binary, target) as facade_url,
    ):
        app = FastAPI()
        app.include_router(
            create_router(
                facade_url, token_file, "https://owner.example", require_owner=require_owner, auth=BearerAuth([])
            )
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://owner.example") as http:
            assert (await http.get("/api/v1/instances")).status_code == 401
            listing = await http.get("/api/v1/instances", headers={"x-owner": "yes"})
            assert listing.status_code == 200
            assert listing.json()["instances"][0]["ref"]["instanceId"] == "account"
            path = "/api/v1/instances/account:importSession"
            body = {"mediaType": "application/json", "formatVersion": "1", "candidate": "e30="}
            denied = await http.post(path, json=body, headers={"x-owner": "yes", "origin": "https://evil.example"})
            assert denied.status_code == 403
            client.import_session.assert_not_awaited()
            result = await http.post(path, json=body, headers={"x-owner": "yes", "origin": "https://owner.example"})
            assert result.status_code == 200
            assert "installed" in result.json()
            client.import_session.assert_awaited_once_with(b"{}", media_type="application/json", format_version="1")
            client.import_session.reset_mock()
            client.import_session.side_effect = OSError("private detail")
            failure = await http.post(path, json=body, headers={"x-owner": "yes", "origin": "https://owner.example"})
            assert failure.status_code == 500
            assert "private detail" not in failure.text
            client.import_session.assert_awaited_once()


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
