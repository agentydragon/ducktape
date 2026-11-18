"""Tests for the scaffold shared by the standalone data-plane proxies."""

from __future__ import annotations

import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

from props.data_plane import create_serve_cli


def test_serve_runs_the_app_on_the_requested_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The image entrypoint runs `serve --host 0.0.0.0`. A single-command Typer (no callback)
    rejects `serve` as an unexpected argument, and a mis-wired option would bind the loopback."""
    app = FastAPI()
    runs: list[tuple[FastAPI, str, int]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, *, host, port: runs.append((app, host, port)))

    result = CliRunner().invoke(
        create_serve_cli(name="test proxy", create_app=lambda: app), ["serve", "--host", "0.0.0.0", "--port", "9000"]
    )

    assert result.exit_code == 0, result.output
    assert runs == [(app, "0.0.0.0", 9000)]


if __name__ == "__main__":
    pytest_bazel.main()
