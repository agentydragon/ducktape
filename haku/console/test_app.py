"""Integration test: the FastAPI JSON API (config read, CSP framing, cache policy).

The console is now the trusted shell — config + the capability tier (tested in
test_capabilities.py). There is no git-write path left to exercise here.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from haku.console import app
from haku.console.conftest import write_config


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_metrics_served_without_operator_session(client) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    # Prometheus scrapes unauthenticated; the token-request histogram is what makes a wedged
    # OAuth association diagnosable, so its absence would make scraping pointless.
    assert "haku_mcp_oauth_token_request_duration_seconds" in response.text


def test_metrics_not_swallowed_by_spa_fallback(make_client, tmp_path: Path) -> None:
    """The dev SPA fallback claims every unmatched path; /metrics must outrank it."""
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_bytes(b"<!doctype html><title>shell</title>")
    with make_client(static_dir=static_dir) as c:
        assert "haku_mcp_oauth_token_request_duration_seconds" in c.get("/metrics").text


def test_config_launch_routine_none_when_unconfigured(client) -> None:
    data = client.get("/api/config").json()
    assert data["launch_routine_url"] is None  # no launch capability configured
    assert data["haku_ui_url"] == "https://haku-ui.test"  # required → always present


def test_config_haku_ui_url_surfaced_and_csp_allows_framing_it(make_operator_client) -> None:
    ui = "https://haku-ui.example.test"
    auth_origin = "https://auth.example.test"
    with make_operator_client(haku_ui_url=ui, auth_origin=auth_origin) as c:
        resp = c.get("/api/config")
        assert resp.json()["haku_ui_url"] == ui
        csp = resp.headers["content-security-policy"]
        assert f"frame-src 'self' {ui} {auth_origin}" in csp
        assert "frame-ancestors 'none'" in csp
        assert resp.headers["referrer-policy"] == "no-referrer"
        # Geolocation and screen capture are scoped to the shell origin — never delegated to the
        # framed haku-ui.
        assert resp.headers["permissions-policy"] == "geolocation=(self), display-capture=(self)"


def test_config_advertises_codex_and_explicit_launch_preserves_public_coder_isolation(
    make_operator_client, tmp_path: Path
) -> None:
    haku_id = "00000000-0000-4000-8000-000000000001"
    coder_id = "00000000-0000-4000-8000-000000000002"
    claude_prompt = tmp_path / "claude.md.j2"
    coder_prompt = tmp_path / "coder.md.j2"
    claude_prompt.write_text("Haku session {{ session_id }} in {{ workspace }}", encoding="utf-8")
    coder_prompt.write_text("Public coder session {{ session_id }} in {{ workspace }}", encoding="utf-8")
    codex_runtime = {
        "agent_id": coder_id,
        "namespace": "codex",
        "warm_pool": "codex",
        "claim_prefix": "codex",
        "harness_label": "codex",
        "cwd": "/test/workspace",
        "session_ttl_seconds": 300,
        "https_proxy": "http://coder-proxy.test:8080",
        "ca_bundle": "/coder-ca.pem",
        "no_proxy": "localhost",
        "mcp_url": "https://console.test/mcp",
        "implementation": {
            "kind": "codex_app_server",
            "model": "codex-test",
            "provider_id": "test-provider",
            "provider_name": "Test OpenAI-compatible provider",
            "api_base_url": "http://litellm.test/v1",
            "api_key_env_var": "OPENAI_API_KEY",
            "github_token_placeholder": "proxy-placeholder",
        },
    }
    shared_config: dict[str, Any] = {
        "harnesses": {
            "claude_code": {
                "agent_id": haku_id,
                "namespace": "claude",
                "warm_pool": "claude",
                "claim_prefix": "claude",
                "harness_label": "claude",
                "cwd": "/test/workspace",
                "session_ttl_seconds": 300,
                "https_proxy": "http://claude-proxy.test:8080",
                "ca_bundle": "/claude-ca.pem",
                "no_proxy": "localhost",
                "mcp_url": "https://console.test/mcp",
                "implementation": {
                    "kind": "claude_code",
                    "api_base_url": "http://litellm.test:4000",
                    "model": "anthropic-max20/ant-messages/claude-sonnet-5",
                    "haiku_model": "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
                    "auth_token_placeholder": "placeholder",
                },
            },
            "codex_app_server": codex_runtime,
        },
        "auto_approval_policies": [{"id": "manual", "type": "never"}],
        "access_profiles": [
            {"id": "haku", "auto_approval_policy": "manual", "allowed_harnesses": ["claude_code"]},
            {"id": "public-coder", "auto_approval_policy": "manual", "allowed_harnesses": ["codex_app_server"]},
        ],
        "default_access_profile_id": "haku",
        "static_agents": {
            "haku": {
                "agent_id": haku_id,
                "display_name": "Haku",
                "token": "haku-token",
                "operator_subject": "operator-sub",
                "access_profile_id": "haku",
            },
            "public_coder": {
                "agent_id": coder_id,
                "display_name": "public-coder-agent",
                "token": "coder-token",
                "operator_subject": "operator-sub",
                "access_profile_id": "public-coder",
            },
        },
        "launchable_agents": [
            {"agent_id": haku_id, "system_prompt_template": str(claude_prompt)},
            {"agent_id": coder_id, "system_prompt_template": str(coder_prompt)},
        ],
    }
    config_file = write_config(tmp_path / "console.yaml", shared_config)

    with make_operator_client(config_file=config_file) as client:
        options = client.get("/api/config").json()["launch_options"]
        assert [(option["agent_id"], option["harness_kind"]) for option in options] == [
            (haku_id, "claude_code"),
            (coder_id, "codex_app_server"),
        ]
        assert all(
            set(option) == {"agent_id", "agent_display_name", "harness_kind", "harness_display_name"}
            for option in options
        )
        assert [(option["agent_display_name"], option["harness_display_name"]) for option in options] == [
            ("Haku", "Claude"),
            ("public-coder-agent", "Codex"),
        ]

        codex = client.post("/api/conversations", json={"agent_id": coder_id, "harness_kind": "codex_app_server"})
        assert codex.status_code == 201
        assert codex.json()["agent_id"] == coder_id
        assert codex.json()["harness_kind"] == "codex_app_server"

        forbidden = client.post("/api/conversations", json={"agent_id": coder_id, "harness_kind": "claude_code"})
        assert forbidden.status_code == 403

        assert client.post("/api/conversations").status_code == 422
        assert client.post("/api/conversations", json={"agent_id": haku_id}).status_code == 422
        assert client.post("/api/conversations", json={"harness_kind": "claude_code"}).status_code == 422

    wrong_codex_slot = copy.deepcopy(shared_config)
    wrong_codex_slot["harnesses"]["codex_app_server"]["implementation"] = {
        "kind": "claude_code",
        "api_base_url": "http://litellm.test:4000",
        "model": "anthropic-max20/ant-messages/claude-sonnet-5",
        "haiku_model": "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
        "auth_token_placeholder": "placeholder",
    }
    wrong_codex_file = write_config(tmp_path / "console-wrong-codex-slot.yaml", wrong_codex_slot)
    with (
        pytest.raises(ValueError, match="codex_app_server must select the codex_app_server implementation"),
        make_operator_client(config_file=wrong_codex_file),
    ):
        pass

    shared_profile_config = copy.deepcopy(shared_config)
    shared_profile_config["static_agents"]["other"] = {
        "agent_id": "00000000-0000-4000-8000-000000000003",
        "display_name": "other-public-coder-shell",
        "token": "other-token",
        "operator_subject": "operator-sub",
        "access_profile_id": "public-coder",
    }
    shared_profile_file = write_config(tmp_path / "console-shared-codex-profile.yaml", shared_profile_config)
    with (
        pytest.raises(ValueError, match="dedicated access profile"),
        make_operator_client(config_file=shared_profile_file),
    ):
        pass


def test_deployment_metadata_comes_from_runtime_image_tags(make_operator_client, monkeypatch) -> None:
    monkeypatch.setenv("HAKU_CONSOLE__IMAGE_TAG", "devel-20260713014452-83da566")
    monkeypatch.setenv("HAKU_CONSOLE__STATIC_IMAGE_TAG", "devel-20260713015518-bfad4bf")

    with make_operator_client() as c:
        assert c.get("/api/deployment").json() == {
            "server": {
                "image_tag": "devel-20260713014452-83da566",
                "source_commit": "83da566",
                "source_commit_url": "https://github.com/agentydragon/ducktape/commit/83da566",
            },
            "frontend": {
                "image_tag": "devel-20260713015518-bfad4bf",
                "source_commit": "bfad4bf",
                "source_commit_url": "https://github.com/agentydragon/ducktape/commit/bfad4bf",
            },
        }


def test_cache_policy_splits_hashed_assets_app_shell_and_api(make_operator_client, tmp_path: Path) -> None:
    static_dir = tmp_path / "web"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    (assets_dir / "main-abcdef123456.js").write_text("console.log('haku')", encoding="utf-8")

    with make_operator_client(static_dir=static_dir) as c:
        assert c.get("/_console/assets/main-abcdef123456.js").headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        missing_asset = c.get("/_console/assets/missing.js")
        assert missing_asset.status_code == 404
        assert missing_asset.headers["cache-control"] == "no-store"
        shell = c.get("/")
        assert shell.headers["cache-control"] == "no-store"
        assert "etag" not in shell.headers
        assert "last-modified" not in shell.headers
        conditional = c.get(
            "/", headers={"If-None-Match": '"old-shell"', "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT"}
        )
        assert conditional.status_code == 200
        assert conditional.headers["cache-control"] == "no-store"
        assert c.get("/api/config").headers["cache-control"] == "no-store"
        assert c.get("/healthz").headers["cache-control"] == "no-store"


def test_spa_route_deep_link_serves_index(make_client, tmp_path: Path) -> None:
    # Console pages and mirrored Haku UI routes have no file on disk; the dev fallback must
    # serve the SPA shell for both, matching production's nginx try_files.
    static_dir = tmp_path / "web"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")

    with make_client(static_dir=static_dir) as c:
        for path in ("/_console/settings", "/_console/tool-calls", "/tool-calls", "/garden/a.md"):
            resp = c.get(path)
            assert resp.status_code == 200
            assert "id='root'" in resp.text
            assert resp.headers["cache-control"] == "no-store"


def test_image_command_dispatches_migration_without_starting_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(app, "migration_main", lambda: called.append("migrate"))
    monkeypatch.setattr(app, "main", lambda: called.append("serve"))

    app.run_command(["migrate"])

    assert called == ["migrate"]


def test_server_startup_checks_schema_without_applying_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    class DatabaseUrl:
        @staticmethod
        def get_secret_value() -> str:
            return "postgresql+asyncpg://approval_store:secret@db.example/approval_store"

    class TestSettings:
        database_url = DatabaseUrl()

    checked: list[str] = []

    async def serve_without_binding(_app: object) -> None:
        pass

    monkeypatch.setattr(app, "Settings", TestSettings)
    monkeypatch.setattr(app, "load_static_agents", lambda settings: [])
    monkeypatch.setattr(app, "verify_schema", checked.append)
    monkeypatch.setattr(app, "create_app", lambda settings, loaded_static_agents: object())
    # The schema check under test runs before main() serves; stub the serve step so the test
    # neither binds a port nor enters the event loop.
    monkeypatch.setattr(app, "_serve", serve_without_binding)

    app.main()

    assert checked == ["postgresql+asyncpg://approval_store:secret@db.example/approval_store"]


def test_image_command_rejects_unknown_modes() -> None:
    with pytest.raises(SystemExit, match="usage"):
        app.run_command(["unknown"])


if __name__ == "__main__":
    pytest_bazel.main()
