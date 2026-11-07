"""The host manages synthetic integrations without owning provider credentials."""

import asyncio
import json
from dataclasses import replace
from pathlib import PurePosixPath

import pytest
import pytest_bazel
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from platform_sessions.auth import Bearer, BearerAuth, BearerConfig
from platform_sessions.host import IntegrationError, create_app
from platform_sessions.registry import IntegrationInstance


class FakeSettings(BaseModel):
    pass


class FakeIntegration:
    def __init__(self):
        self.config = IntegrationInstance(
            "example", "example", True, "https://example.test", PurePosixPath("/data/example"), 900, FakeSettings()
        )
        self.periodic = False
        self.candidate_verification = True
        self.session = None
        self.checks = 0

    async def metadata(self):
        return {"id": self.config.id, "type": self.config.type, "instructions": []}

    def parse(self, body):
        value = json.loads(body)
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise ValueError("invalid")
        return value["token"]

    async def status(self):
        return {"state": "ready" if self.session else "uninitialized"}

    async def refresh(self, transport=None, *, source="verify"):
        self.checks += 1

    async def check_candidate(self, candidate, transport=None):
        if candidate == "bad-secret":
            raise IntegrationError("reauth_required", 409)

    async def install(self, candidate, transport=None):
        await self.check_candidate(candidate, transport)
        self.session = candidate
        return {"configured": True}


def owner(request: Request):
    if request.cookies.get("owner") != "yes":
        raise HTTPException(status_code=401)


def test_owner_api_and_candidate_verification():
    integration = FakeIntegration()
    app = create_app([integration], "https://service.test", require_owner=owner, auth=BearerAuth([]))
    with TestClient(app, base_url="https://service.test") as client:
        assert client.get("/api/integrations").status_code == 401
        headers = {"cookie": "owner=yes", "origin": "https://service.test"}
        response = client.get("/api/integrations", headers=headers)
        assert response.json() == [{"id": "example", "type": "example", "instructions": []}]
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/api/integrations/example/session", headers={"cookie": "owner=yes"}, json={"token": "x"}
            ).status_code
            == 403
        )
        checked = client.post("/api/integrations/example/session/verify", headers=headers, json={"token": "candidate"})
        assert checked.json() == {"outcome": "success"}
        assert integration.session is None
        rejected = client.post("/api/integrations/example/session", headers=headers, json={"token": "bad-secret"})
        assert rejected.status_code == 409
        assert "bad-secret" not in rejected.text
        assert integration.session is None
        assert client.post("/api/integrations/example/session", headers=headers, json={"token": "accepted"}).json() == {
            "configured": True
        }
        assert client.get("/api/integrations/example", headers=headers).json() == {"state": "ready"}
        assert client.post("/api/integrations/example/verify", headers=headers).status_code == 200
        assert integration.checks == 1
        assert client.get("/").status_code == 200
        assert client.get("/status.js").status_code == 200
        assert client.get("/status.css").status_code == 200


def test_list_awaits_metadata_concurrently_and_preserves_configured_order():
    second_started = asyncio.Event()

    class WaitingIntegration(FakeIntegration):
        async def metadata(self):
            await asyncio.wait_for(second_started.wait(), timeout=1)
            return await super().metadata()

    class SignalingIntegration(FakeIntegration):
        async def metadata(self):
            second_started.set()
            return await super().metadata()

    first = WaitingIntegration()
    second = SignalingIntegration()
    second.config = replace(second.config, id="second")
    app = create_app([first, second], "https://service.test", require_owner=owner, auth=BearerAuth([]))
    with TestClient(app) as client:
        response = client.get("/api/integrations", headers={"cookie": "owner=yes"})
    assert response.status_code == 200
    assert response.json() == [
        {"id": "example", "type": "example", "instructions": []},
        {"id": "second", "type": "example", "instructions": []},
    ]


def test_candidate_limits_and_unsupported_verification():
    integration = FakeIntegration()
    integration.candidate_verification = False
    with TestClient(
        create_app([integration], "https://service.test", require_owner=owner, auth=BearerAuth([]))
    ) as client:
        headers = {"cookie": "owner=yes", "origin": "https://service.test"}
        assert (
            client.post("/api/integrations/example/session/verify", headers=headers, json={"token": "x"}).status_code
            == 404
        )
        assert (
            client.post("/api/integrations/example/session", headers=headers, json={"token": "x" * 65536}).status_code
            == 400
        )
        assert client.post("/api/integrations/unknown/session", headers=headers, json={"token": "x"}).status_code == 404


def test_owner_and_limited_bearer_privileges():
    integration = FakeIntegration()
    auth = BearerAuth([Bearer(SecretStr("owner-token"), owner=True), Bearer(SecretStr("consumer-token"))])
    app = create_app([integration], "https://service.test", require_owner=owner, auth=auth)
    with TestClient(app, base_url="https://service.test") as client:
        owner_auth = {"authorization": "Bearer owner-token"}
        limited = {"authorization": "Bearer consumer-token", "cookie": "owner=yes"}
        assert client.get("/api/integrations", headers=limited).status_code == 403
        assert client.get("/api/integrations", headers=owner_auth).status_code == 200
        path = "/api/integrations/example/session"
        assert client.post(path, headers=owner_auth, json={"token": "ok"}).status_code == 200
        assert (
            client.post(path, headers=owner_auth | {"origin": "https://evil.test"}, json={"token": "x"}).status_code
            == 403
        )
        assert (
            client.post(path, headers=owner_auth | {"sec-fetch-site": "cross-site"}, json={"token": "x"}).status_code
            == 403
        )
        assert client.post(path, headers=limited, json={"token": "x"}).status_code == 403
        assert integration.session == "ok"
    with pytest.raises(ValueError, match="duplicate"):
        BearerAuth([Bearer(SecretStr("same")), Bearer(SecretStr("same"), owner=True)])


def test_bearer_config_reads_a_mounted_secret(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("s" * 32 + "\n")
    config = BearerConfig(role="owner", token_file=token_file)
    assert config.load_token().get_secret_value() == "s" * 32
    assert "s" * 32 not in repr(config)
    with pytest.raises(ValueError, match="exactly one"):
        BearerConfig(role="owner")
    with pytest.raises(ValueError, match="exactly one"):
        BearerConfig(role="owner", token=SecretStr("x" * 32), token_file=token_file)
    token_file.write_text("short")
    with pytest.raises(ValueError, match="at least 32"):
        config.load_token()


def test_bearer_config_rejects_unknown_instances():
    config = BearerConfig.model_validate(
        {"token": "x" * 32, "role": "routes", "allow": [{"instance": "missing", "method": "GET", "path": "/orders"}]}
    )
    with pytest.raises(ValueError, match="unknown or disabled"):
        BearerAuth.from_config({"consumer": config}, instances={"example"})


def test_failed_start_still_stops_owner():
    calls = []

    async def start():
        calls.append("start")
        raise RuntimeError("startup failed")

    async def stop():
        calls.append("stop")

    app = create_app([], "https://service.test", require_owner=owner, auth=BearerAuth([]), start=start, stop=stop)
    with pytest.raises(RuntimeError, match="startup failed"), TestClient(app):
        pass
    assert calls == ["start", "stop"]


if __name__ == "__main__":
    pytest_bazel.main()
