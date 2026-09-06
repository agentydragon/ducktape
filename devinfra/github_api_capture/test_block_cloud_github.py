import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_bazel
from mitmproxy import connection, ctx, http

from devinfra.github_api_capture.block_cloud_github import BlockCloudGithub


@pytest.mark.parametrize(
    ("method", "url", "matches"),
    [
        ("POST", "https://claude.ai/v1/code/github/batch-branch-status", True),
        ("POST", "https://claude.ai/v1/code/github/batch-branch-status?caller=session-sidebar", True),
        ("GET", "https://claude.ai/v1/code/github/batch-branch-status", False),
        ("POST", "https://other.test/v1/code/github/batch-branch-status", False),
        ("POST", "https://claude.ai/v1/code/github/batch-branch-status/other", False),
        ("POST", "https://claude.ai/v1/code/sessions", False),
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
def test_exact_opt_in_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, url: str, matches: bool, enabled: bool
) -> None:
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(
        ctx, "options", SimpleNamespace(block_cloud_github_batch=enabled, cloud_github_block_events=str(events)), raising=False
    )
    addon = BlockCloudGithub()
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
        connection.Server(address=("claude.ai", 443)),
    )
    flow.request = http.Request.make(
        method, url, b"test-private-body", headers=http.Headers(authorization="test-private-token")
    )
    addon.request(flow)
    if enabled and matches:
        assert flow.response is not None
        assert flow.response.status_code == 429
        assert flow.response.headers["retry-after"] == "3600"
        record = json.loads(events.read_text())
        assert record["event"] == "blocked"
        assert record["blocked_requests"] == 1
        assert "test-private" not in events.read_text()
    else:
        assert flow.response is None
        assert addon.blocked_requests == 0
        assert not events.exists()


def test_metadata_appends_and_identifies_process_lifetime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(
        ctx, "options", SimpleNamespace(block_cloud_github_batch=True, cloud_github_block_events=str(events)), raising=False
    )
    addon = BlockCloudGithub()
    addon.record("started")
    addon.record("heartbeat")
    addon.done()
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["started", "heartbeat", "stopped"]
    assert all(row["started_at"] == addon.started_at for row in rows)
    assert all(row["enabled"] is True for row in rows)
    assert all(row["blocked_requests"] == 0 for row in rows)


if __name__ == "__main__":
    pytest_bazel.main()
