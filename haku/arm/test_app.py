"""Integration test: the FastAPI app over a seeded local haku-state clone."""

from __future__ import annotations

import pytest_bazel
from fastapi.testclient import TestClient

from haku.arm.app import create_app


def test_healthz_and_index(seeded) -> None:
    app = create_app(seeded.settings, git_state=seeded.git_state)
    # TestClient's context manager runs the lifespan → clones the seeded remote.
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        resp = client.get("/")
        assert resp.status_code == 200
        assert '<details class="task">' in resp.text
        for title in seeded.titles:
            assert title in resp.text


if __name__ == "__main__":
    pytest_bazel.main()
