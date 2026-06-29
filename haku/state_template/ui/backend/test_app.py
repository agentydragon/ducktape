"""The FastAPI surface — health, the two read endpoints, and the trace writes.

The Forgejo client is replaced with a fake via dependency override, so these test the
HTTP contract (status, JSON shape, that writes reach the client) without a real Forgejo.
"""

from __future__ import annotations

from app import _forgejo, create_app
from config import Settings
from fastapi.testclient import TestClient
from models import Item, ItemStatus


class FakeForgejo:
    """Records calls; serves a fixed dashboard."""

    def __init__(self) -> None:
        self.clicks_set: list[tuple[str, str]] = []
        self.clicks_cleared: list[tuple[str, str]] = []
        self.feedback: list[tuple[str, str | None]] = []

    async def read_dashboard(self):
        item = Item(id="01AAA", title="t", body="b", value=9, status=ItemStatus.OPEN)
        return [item], {("01AAA", "done")}, "2026-06-28T22:00:00Z"

    async def set_click(self, item_id: str, action_id: str) -> None:
        self.clicks_set.append((item_id, action_id))

    async def clear_click(self, item_id: str, action_id: str) -> None:
        self.clicks_cleared.append((item_id, action_id))

    async def write_feedback(self, text: str, item_id: str | None = None) -> None:
        self.feedback.append((text, item_id))

    async def read_improvements(self):
        return {
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
                    "detail": "impact + the fix the operator could make",
                }
            ],
        }


def _client() -> tuple[TestClient, FakeForgejo]:
    settings = Settings(git_username="u", git_password="p")
    app = create_app(settings)
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
    settings = Settings(git_username="u", git_password="p", git_sha="abcdef1234567890")
    app = create_app(settings)
    app.dependency_overrides[_forgejo] = lambda: FakeForgejo()
    with TestClient(app) as client:
        body = client.get("/api/dashboard").json()
    assert body["deployed_commit"] == "abcdef1"
    assert body["deployed_commit_url"] == "https://git.allegedly.works/haku/haku-state/commit/abcdef1234567890"


def test_trace_click_set_and_clear_reach_forgejo():
    client, fake = _client()
    with client:
        assert client.put("/api/trace/items/01AAA/actions/done").status_code == 200
        assert client.delete("/api/trace/items/01AAA/actions/done").status_code == 200
    assert fake.clicks_set == [("01AAA", "done")]
    assert fake.clicks_cleared == [("01AAA", "done")]


def test_trace_feedback_reaches_forgejo():
    client, fake = _client()
    with client:
        r = client.post("/api/trace/feedback", json={"text": "hello", "item_id": "01AAA"})
    assert r.status_code == 200
    assert fake.feedback == [("hello", "01AAA")]


def test_improvements_returns_ideas_and_friction():
    client, _ = _client()
    with client:
        body = client.get("/api/improvements").json()
    assert body["updated"] == "2026-06-29T05:30:00Z"
    assert [i["id"] for i in body["ideas"]] == ["example-idea"]
    assert body["ideas"][0]["value"] == "high"
    assert [f["id"] for f in body["friction"]] == ["example-friction"]
    assert body["friction"][0]["severity"] == "medium"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q"])
