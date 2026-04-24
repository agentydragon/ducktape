"""Backend smoke tests — state round-trip, ETag handling, static 404 behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from x.auragon_study_casino.app import create_app
from x.auragon_study_casino.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "nonexistent_dist")
    return TestClient(create_app(settings))


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_get_state_empty_returns_empty_object(client: TestClient) -> None:
    response = client.get("/state")
    assert response.status_code == 200
    assert response.json() == {}
    assert response.headers["etag"] == '"empty"'


def test_put_then_get_roundtrip(client: TestClient) -> None:
    payload = {"credits": 42, "sessions": []}
    put_response = client.put("/state", content=json.dumps(payload))
    assert put_response.status_code == 204
    etag = put_response.headers["etag"]
    assert etag

    get_response = client.get("/state")
    assert get_response.status_code == 200
    assert get_response.json() == payload
    assert get_response.headers["etag"] == etag


def test_if_match_rejects_stale_write(client: TestClient) -> None:
    client.put("/state", content=json.dumps({"credits": 1}))
    stale_response = client.put("/state", content=json.dumps({"credits": 2}), headers={"If-Match": '"bogus"'})
    assert stale_response.status_code == 412
    # The first blob must still be there.
    assert client.get("/state").json() == {"credits": 1}


def test_if_match_accepts_matching_etag(client: TestClient) -> None:
    first = client.put("/state", content=json.dumps({"credits": 1}))
    second = client.put("/state", content=json.dumps({"credits": 2}), headers={"If-Match": first.headers["etag"]})
    assert second.status_code == 204
    assert client.get("/state").json() == {"credits": 2}


def test_empty_body_rejected(client: TestClient) -> None:
    response = client.put("/state", content=b"")
    assert response.status_code == 400


if __name__ == "__main__":
    pytest_bazel.main()
