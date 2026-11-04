"""The host manages synthetic integrations without owning provider credentials."""

import json
from pathlib import PurePosixPath

import pytest_bazel
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from platform_sessions.registry import IntegrationInstance
from platform_sessions.service import IntegrationError, create_app


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

    def metadata(self):
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
    if request.headers.get("authorization") != "Bearer owner":
        raise HTTPException(status_code=401)


def test_owner_api_and_candidate_verification():
    integration = FakeIntegration()
    app = create_app([integration], "https://service.test", require_owner=owner)
    with TestClient(app, base_url="https://service.test") as client:
        assert client.get("/api/integrations").status_code == 401
        headers = {"authorization": "Bearer owner", "origin": "https://service.test"}
        response = client.get("/api/integrations", headers=headers)
        assert response.json() == [{"id": "example", "type": "example", "instructions": []}]
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/api/integrations/example/session", headers={"authorization": "Bearer owner"}, json={"token": "x"}
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


def test_candidate_limits_and_unsupported_verification():
    integration = FakeIntegration()
    integration.candidate_verification = False
    with TestClient(create_app([integration], "https://service.test", require_owner=owner)) as client:
        headers = {"authorization": "Bearer owner", "origin": "https://service.test"}
        assert (
            client.post("/api/integrations/example/session/verify", headers=headers, json={"token": "x"}).status_code
            == 404
        )
        assert (
            client.post("/api/integrations/example/session", headers=headers, json={"token": "x" * 65536}).status_code
            == 400
        )
        assert client.post("/api/integrations/unknown/session", headers=headers, json={"token": "x"}).status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
