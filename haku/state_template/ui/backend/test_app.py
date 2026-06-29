"""The FastAPI surface — health, the two read endpoints, and the trace writes.

The Forgejo client is replaced with a fake via dependency override. The fake implements the
**generic** Forgejo primitives the endpoints compose (commits/tree/blobs for the items-board
read, read_yaml for improvements, create_file/delete_file for the trace writes) — so these
tests exercise the real feature layer (reads.read_dashboard + the endpoints) over canned git
content, without a real Forgejo.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app import _forgejo, create_app
from config import Settings

_ITEM_YAML = b'id: "01AAA"\ntitle: "t"\nbody: "b"\nvalue: 9\nstatus: open\n'
_IMPROVEMENTS = {
    "updated": "2026-06-29T05:30:00Z",
    "ideas": [
        {
            "id": "example-idea",
            "title": "An example capability idea",
            "value": "high",
            "status": "recommend",
            "summary": "what it would unlock",
            "detail": "**why** it matters",
        }
    ],
    "friction": [
        {
            "id": "example-friction",
            "title": "An example data-access gap",
            "severity": "medium",
            "status": "open",
            "detail": "impact + the fix",
        }
    ],
}


class FakeForgejo:
    """Fakes the generic Forgejo content primitives; records writes."""

    def __init__(self) -> None:
        self.created: list[tuple[str, bytes, str]] = []
        self.deleted: list[tuple[str, str]] = []

    async def commits(self, limit: int = 40) -> list[dict[str, Any]]:
        return [
            {"sha": "HEAD", "commit": {"author": {"email": "haku@allegedly.works", "date": "2026-06-28T22:00:00Z"}}}
        ]

    async def tree(self, sha: str) -> list[dict[str, Any]]:
        return [
            {"type": "blob", "path": "items/01AAA.yaml", "sha": "sha-a"},
            {"type": "blob", "path": "clicks/01AAA/done", "sha": "sha-c"},
        ]

    async def blobs(self, shas: list[str]) -> list[bytes]:
        return [_ITEM_YAML for _ in shas]

    async def read_yaml(self, path: str) -> Any:
        assert path == "improvements.yaml"
        return _IMPROVEMENTS

    async def create_file(self, path: str, content: bytes, message: str) -> None:
        self.created.append((path, content, message))

    async def delete_file(self, path: str, message: str) -> None:
        self.deleted.append((path, message))


def _settings(**overrides: Any) -> Settings:
    base = {
        "forgejo_api_url": "http://forgejo.test/api/v1/repos/haku/haku-state",
        "repo_web_url": "https://git.example/haku/haku-state",
        "git_username": "u",
        "git_password": "p",
    }
    return Settings(**{**base, **overrides})


def _client(**setting_overrides: Any) -> tuple[TestClient, FakeForgejo]:
    app = create_app(_settings(**setting_overrides))
    fake = FakeForgejo()
    app.dependency_overrides[_forgejo] = lambda: fake
    return TestClient(app), fake


def test_healthz():
    client, _ = _client()
    with client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_dashboard_returns_items_and_clicks():
    client, _ = _client()
    with client:
        r = client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert [i["id"] for i in body["items"]] == ["01AAA"]
    assert body["clicks"] == [{"item_id": "01AAA", "action_id": "done"}]
    assert body["scan_time"] == "2026-06-28T22:00:00Z"
    assert body["deployed_commit"] is None  # no git_sha in this Settings


def test_dashboard_surfaces_deployed_commit_when_git_sha_set():
    client, _ = _client(git_sha="abcdef1234567890")
    with client:
        body = client.get("/api/dashboard").json()
    assert body["deployed_commit"] == "abcdef1"
    assert body["deployed_commit_url"] == "https://git.example/haku/haku-state/commit/abcdef1234567890"


def test_improvements_returns_ideas_and_friction():
    client, _ = _client()
    with client:
        body = client.get("/api/improvements").json()
    assert body["updated"] == "2026-06-29T05:30:00Z"
    assert [i["id"] for i in body["ideas"]] == ["example-idea"]
    assert body["ideas"][0]["value"] == "high"
    assert [f["id"] for f in body["friction"]] == ["example-friction"]


def test_trace_click_set_and_clear_reach_forgejo():
    client, fake = _client()
    with client:
        assert client.put("/api/trace/items/01AAA/actions/done").status_code == 200
        assert client.delete("/api/trace/items/01AAA/actions/done").status_code == 200
    assert [p for p, _c, _m in fake.created] == ["clicks/01AAA/done"]
    assert [p for p, _m in fake.deleted] == ["clicks/01AAA/done"]


def test_trace_feedback_reaches_forgejo():
    client, fake = _client()
    with client:
        r = client.post("/api/trace/feedback", json={"text": "hello", "item_id": "01AAA"})
    assert r.status_code == 200
    path, content, _msg = fake.created[0]
    assert path.endswith("-feedback-01AAA.md")
    assert b"hello" in content


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q"])
