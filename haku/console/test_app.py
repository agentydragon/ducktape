"""Integration test: the FastAPI JSON API (config read, CSP framing, cache policy).

The console is now the trusted shell — config + the capability tier (tested in
test_capabilities.py). There is no git-write path left to exercise here.
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


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


def test_deployment_metadata_comes_from_runtime_image_tags(make_operator_client, monkeypatch) -> None:
    monkeypatch.setenv("HAKU_CONSOLE_IMAGE_TAG", "devel-20260713014452-83da566")
    monkeypatch.setenv("HAKU_CONSOLE_STATIC_IMAGE_TAG", "devel-20260713015518-bfad4bf")

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


if __name__ == "__main__":
    pytest_bazel.main()
