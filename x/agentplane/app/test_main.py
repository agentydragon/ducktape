"""The SPA mount's cache contract."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from x.agentplane.app.main import SpaFiles

# gazelle:include_dep @pypi//httpx


def test_spa_files_are_never_reused_from_a_browser_cache(tmp_path: Path) -> None:
    """Bazel's fixed mtimes would otherwise validate a stale bundle: no-store, and no 304."""
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "main.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/", SpaFiles(directory=tmp_path, html=True), name="frontend")
    client = TestClient(app)

    first = client.get("/main.js")
    again = client.get("/main.js", headers={"If-None-Match": first.headers.get("etag", "*")})
    shell = client.get("/")

    assert first.headers["cache-control"] == "no-store"
    assert "etag" not in first.headers
    assert again.status_code == 200
    assert (shell.status_code, shell.headers["cache-control"], shell.headers["content-type"]) == (
        200,
        "no-store",
        "text/html; charset=utf-8",
    )


if __name__ == "__main__":
    pytest_bazel.main()
