"""The SPA mount's cache contract, and the two settings models the Deployment's environment feeds."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from x.agentplane.app.main import Settings, SpaFiles
from x.agentplane.app.oidc import load_settings

# gazelle:include_dep @pypi//httpx

APP_ENVIRONMENT = {
    "AGENTPLANE_NAMESPACE": "test-namespace",
    "AGENTPLANE_TEMPLATE": "test-template",
    "AGENTPLANE_RUNNER_PORT": "7000",
    "AGENTPLANE_DATABASE_URL": "postgresql+asyncpg://test@test.invalid/test",
    "AGENTPLANE_MODELS": '{"claude": ["test-claude-model"], "codex": ["test-codex-model"]}',
    "AGENTPLANE_EGRESS_ADMIN_URL": "http://egress.test.invalid:8081",
}
OIDC_ENVIRONMENT = {
    "AGENTPLANE_OIDC_ISSUER": "https://auth.test.invalid/application/o/test-app/",
    "AGENTPLANE_OIDC_CLIENT_ID": "test-client",
    "AGENTPLANE_OIDC_CLIENT_SECRET": "test-client-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_SESSION_SECRET": "test-session-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_PUBLIC_BASE_URL": "https://app.test.invalid",
}


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


def test_the_two_settings_models_read_one_environment_without_colliding(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AGENTPLANE_OIDC_` sits inside `AGENTPLANE_`, and the staging Deployment sets both.

    A model that claimed the other's variables, or refused to parse alongside them, would take the
    app down at rollout rather than here.
    """
    for name, value in (APP_ENVIRONMENT | OIDC_ENVIRONMENT).items():
        monkeypatch.setenv(name, value)

    settings = Settings(_cli_parse_args=[])
    oidc = load_settings()

    assert (settings.namespace, settings.port, settings.token_audience) == ("test-namespace", 8080, "agentplane")
    assert oidc is not None
    assert oidc.redirect_uri == "https://app.test.invalid/auth/callback"
    assert (
        oidc.server_metadata_url == "https://auth.test.invalid/application/o/test-app/.well-known/openid-configuration"
    )
    # https, so the cookie takes the __Host- prefix that binds it to this exact origin.
    assert oidc.cookie_name.startswith("__Host-")


def test_without_an_issuer_there_is_no_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app is guarded either way; unset, what is missing is the browser's way to get a session."""
    for name in OIDC_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    assert load_settings() is None


if __name__ == "__main__":
    pytest_bazel.main()
