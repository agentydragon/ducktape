"""The FastAPI surface — health, the read endpoints, and the trace/response writes.

The Forgejo client is replaced with a fake via dependency override. The fake implements the
**generic** Forgejo primitives the endpoints compose (commits/tree/blobs for the content proxy,
create_file/write_file/delete_file for writes) — so these tests exercise the real feature layer
(reads.py + the endpoints) over canned git content, without a real Forgejo.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest_bazel
from app import _forgejo, create_app
from config import Settings
from fastapi.testclient import TestClient


class FakeForgejo:
    """Fakes the generic Forgejo content primitives; records writes."""

    def __init__(self) -> None:
        self.created: list[tuple[str, bytes, str]] = []
        self.written: list[tuple[str, bytes, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.yaml_docs: dict[str, Any] = {}

    async def commits(self, limit: int = 40) -> list[dict[str, Any]]:
        return [{"sha": "HEAD", "commit": {"author": {"email": "haku@example.com", "date": "2026-06-28T22:00:00Z"}}}]

    async def tree(self, sha: str) -> list[dict[str, Any]]:
        # The markdown collection members carry realistic hex shas (the /api/repo/blobs endpoint
        # validates shas as hex).
        return [
            {"type": "blob", "path": "memory/improvements/beta.md", "sha": "b2b2"},
            {"type": "blob", "path": "memory/improvements/alpha.md", "sha": "a1a1"},
        ]

    async def blobs(self, shas: list[str]) -> list[bytes]:
        content = {
            "a1a1": b"---\nkind: improvement\ntitle: Alpha\n---\nalpha body\n",
            "b2b2": b"---\nkind: improvement\ntitle: Beta\n---\nbeta body\n",
        }
        return [content[s] for s in shas]

    async def read_yaml(self, path: str) -> Any | None:
        return self.yaml_docs.get(path)

    async def create_file(self, path: str, content: bytes, message: str) -> None:
        self.created.append((path, content, message))

    async def write_file(self, path: str, content: bytes, message: str) -> None:
        self.written.append((path, content, message))

    async def delete_file(self, path: str, message: str) -> None:
        self.deleted.append((path, message))


def _settings(**overrides: Any) -> Settings:
    base = {
        "forgejo_api_url": "http://forgejo.test/api/v1/repos/haku/haku-state",
        "repo_web_url": "https://your-forgejo.example.com/haku/haku-state",
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


def test_meta_returns_scan_time():
    client, _ = _client()
    with client:
        body = client.get("/api/meta").json()
    assert body["scan_time"] == "2026-06-28T22:00:00Z"  # newest Haku-authored commit
    assert body["deployed_commit"] is None  # no git_sha in this Settings


def test_meta_surfaces_deployed_commit_when_git_sha_set():
    client, _ = _client(git_sha="abcdef1234567890")
    with client:
        body = client.get("/api/meta").json()
    assert body["deployed_commit"] == "abcdef1"
    assert body["deployed_commit_url"] == "https://your-forgejo.example.com/haku/haku-state/commit/abcdef1234567890"


def test_trace_feedback_reaches_forgejo():
    client, fake = _client()
    with client:
        r = client.post("/api/trace/feedback", json={"text": "hello", "item_id": "01AAA"})
    assert r.status_code == 200
    path, content, _msg = fake.created[0]
    assert path.endswith("-feedback-01AAA.md")
    assert b"hello" in content


def test_feedback_records_page_and_selection_in_the_note():
    client, fake = _client()
    with client:
        r = client.post(
            "/api/trace/feedback",
            json={"text": "this page looks bad", "page": "#/runs", "selection": "6 scanned · 2 skipped"},
        )
    assert r.status_code == 200
    _path, content, _msg = fake.created[0]
    body = content.decode()
    assert "this page looks bad" in body
    assert "Reported from page: #/runs" in body
    # Selected text is block-quoted so Haku reads it as a quote, not as note prose.
    assert "Selected text:\n> 6 scanned · 2 skipped" in body


def test_feedback_omits_context_block_when_no_page_or_selection():
    client, fake = _client()
    with client:
        client.post("/api/trace/feedback", json={"text": "plain note"})
    _path, content, _msg = fake.created[0]
    # No page/selection → no trailing context rule, just the heading + note.
    assert "Reported from page" not in content.decode()
    assert "---" not in content.decode()


def test_response_set_and_clear_reach_forgejo():
    client, fake = _client()
    with client:
        assert client.put("/api/responses/dentist-appt/status", json={"value": "went"}).status_code == 200
        assert client.delete("/api/responses/dentist-appt/status").status_code == 200
    path, content, _msg = fake.written[0]
    assert path == "responses/dentist-appt/status.yaml"
    assert b"went" in content
    assert [p for p, _m in fake.deleted] == ["responses/dentist-appt/status.yaml"]


def test_response_records_value_note_and_timestamp():
    client, fake = _client()
    with client:
        client.put("/api/responses/york-tender/outcome", json={"value": "other", "note": "rescheduled to Tue"})
    _path, content, _msg = fake.written[0]
    body = content.decode()
    assert "value: other" in body
    assert "note: rescheduled to Tue" in body
    assert "at:" in body


def test_location_accepts_a_valid_fix_without_persisting_to_git():
    # Persistence is a TODO (a time-series store, not git) — the endpoint validates + logs the
    # fix but must NOT commit anything, so haku-state's history stays free of dense location data.
    client, fake = _client()
    with client:
        r = client.post(
            "/api/location",
            json={"latitude": 37.7749, "longitude": -122.4194, "accuracy": 12.5, "timestamp": 1_700_000_000_000},
        )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    # Nothing written to git (persistence is a TODO — a time-series store, not git).
    assert fake.written == []
    assert fake.created == []


def test_location_rejects_out_of_range_coordinates():
    client, _ = _client()
    with client:
        r = client.post(
            "/api/location",
            json={"latitude": 137.0, "longitude": -122.4194, "accuracy": 12.5, "timestamp": 1_700_000_000_000},
        )
    assert r.status_code == 422  # latitude outside [-90, 90]


def test_repo_tree_returns_head_sha_and_recursive_entries():
    client, _ = _client()
    with client:
        body = client.get("/api/repo/tree").json()
    assert body["sha"] == "HEAD"
    by_path = {e["path"]: e for e in body["entries"]}
    assert "memory/improvements/alpha.md" in by_path
    assert by_path["memory/improvements/alpha.md"] == {
        "path": "memory/improvements/alpha.md",
        "type": "blob",
        "sha": "a1a1",
    }


def test_repo_blobs_bulk_fetch_in_input_order():
    client, _ = _client()
    with client:
        body = client.get("/api/repo/blobs", params={"shas": "a1a1,b2b2"}).json()
    assert [b["sha"] for b in body] == ["a1a1", "b2b2"]
    assert "alpha body" in body[0]["content"]
    assert "kind: improvement" in body[1]["content"]


def test_repo_blobs_rejects_non_hex_sha():
    client, _ = _client()
    with client:
        assert client.get("/api/repo/blobs", params={"shas": "a1a1,../etc"}).status_code == 400
        assert client.get("/api/repo/blobs", params={"shas": "nothex"}).status_code == 400


def test_tool_request_call_submits_exact_request_to_console():
    client, _ = _client(haku_console_api_url="https://haku-console.test", haku_console_api_token="console-token")
    request_body = {
        "state_request_id": "2026-07-thrive-box-grocy-stock-add",
        "server_id": "grocy-sf",
        "tool_name": "stock_add",
        "title": "Add arrived Thrive box items to Grocy",
        "rationale": "box present",
        "arguments": {"items": [{"product_id": 123, "amount": 1}]},
        "wait_for_ms": 500,
    }
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    class RecordingAsyncClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            assert base_url == "https://haku-console.test"
            assert timeout == 65.0

        async def __aenter__(self) -> RecordingAsyncClient:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def post(self, path: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
            calls.append((path, json, headers))
            return httpx.Response(
                200,
                json={
                    "tool_call_id": "tc_1",
                    "server_id": "grocy-sf",
                    "tool_name": "stock_add",
                    "status": "pending_approval",
                },
            )

    with client, patch("app.httpx.AsyncClient", RecordingAsyncClient):
        resp = client.post("/api/tool-calls", json=request_body)
    assert resp.status_code == 200
    assert resp.json()["tool_call_id"] == "tc_1"
    path, body, headers = calls[0]
    assert path == "/api/tool-calls"
    assert headers["Authorization"] == "Bearer console-token"
    assert body["server_id"] == "grocy-sf"
    assert body["tool_name"] == "stock_add"
    assert body["arguments"] == {"items": [{"product_id": 123, "amount": 1}]}
    assert body["rationale"] == "box present"
    assert body["title"] == "Add arrived Thrive box items to Grocy"
    assert body["state_request_id"] == "2026-07-thrive-box-grocy-stock-add"
    assert body["client_request_id"] == "haku-state:tool-call:2026-07-thrive-box-grocy-stock-add"
    assert body["wait_for_ms"] == 500


def test_tool_request_call_rejects_missing_console_config():
    client, _ = _client()
    with client:
        resp = client.post(
            "/api/tool-calls", json={"state_request_id": "req", "server_id": "grocy", "tool_name": "x", "title": "X"}
        )
    assert resp.status_code == 503


def test_tool_request_call_rejects_invalid_state_request_id():
    client, _ = _client(haku_console_api_url="https://haku-console.test")
    with client:
        resp = client.post(
            "/api/tool-calls", json={"state_request_id": "../bad", "server_id": "grocy", "tool_name": "x", "title": "X"}
        )
    assert resp.status_code == 400


def test_tool_request_lookup_returns_existing_console_state():
    client, _ = _client(haku_console_api_url="https://haku-console.test", haku_console_api_token="console-token")
    gets: list[tuple[str, dict[str, str]]] = []

    class RecordingAsyncClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            assert base_url == "https://haku-console.test"
            assert timeout == 15.0

        async def __aenter__(self) -> RecordingAsyncClient:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def get(self, path: str, *, headers: dict[str, str]) -> httpx.Response:
            gets.append((path, headers))
            return httpx.Response(200, json={"tool_call_id": "tc_existing", "server_id": "grocy-sf", "status": "ok"})

    with client, patch("app.httpx.AsyncClient", RecordingAsyncClient):
        resp = client.post(
            "/api/tool-calls/lookup",
            json={"state_request_id": "req", "server_id": "grocy-sf", "tool_name": "stock_add", "title": "Add stock"},
        )
    assert resp.status_code == 200
    assert resp.json()["tool_call_id"] == "tc_existing"
    path, headers = gets[0]
    assert path == "/api/tool-calls/by-client-request/haku-state:tool-call:req"
    assert headers["Authorization"] == "Bearer console-token"


if __name__ == "__main__":
    pytest_bazel.main()
