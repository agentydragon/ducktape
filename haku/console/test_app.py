"""Integration test: the FastAPI JSON API over a seeded local haku-state clone.

Write endpoints are exercised against a real (local, bare) git remote so each test
asserts the commit actually landed on the remote with the ``haku-console`` identity
— the console's whole job is to turn an operator action into a git event Haku can reduce.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest_bazel


def _remote_tip(bare: Path) -> pygit2.Commit:
    repo = pygit2.Repository(str(bare))
    return repo[repo.lookup_reference("refs/heads/main").target].peel(pygit2.Commit)


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_config_returns_none_when_unconfigured(client) -> None:
    data = client.get("/api/config").json()
    assert data["launch_routine_url"] is None
    assert data["haku_ui_url"] is None


def test_config_haku_ui_url_surfaced_and_csp_allows_framing_it(make_client) -> None:
    ui = "https://haku-ui.example.test"
    auth_origin = "https://auth.example.test"
    with make_client(haku_ui_url=ui, auth_origin=auth_origin) as c:
        resp = c.get("/api/config")
        assert resp.json()["haku_ui_url"] == ui
        csp = resp.headers["content-security-policy"]
        assert f"frame-src 'self' {ui} {auth_origin}" in csp
        assert "frame-ancestors 'none'" in csp


def test_config_unconfigured_csp_denies_framing(client) -> None:
    resp = client.get("/api/config")
    assert resp.json()["haku_ui_url"] is None
    assert "frame-src 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_cache_policy_splits_hashed_assets_app_shell_and_api(make_client, tmp_path: Path) -> None:
    static_dir = tmp_path / "web"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    (assets_dir / "main-abcdef123456.js").write_text("console.log('haku')", encoding="utf-8")

    with make_client(static_dir=static_dir) as c:
        assert c.get("/assets/main-abcdef123456.js").headers["cache-control"] == ("public, max-age=31536000, immutable")
        assert c.get("/").headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
        assert c.get("/api/config").headers["cache-control"] == "no-store"
        assert c.get("/healthz").headers["cache-control"] == "no-store"


def test_trace_appends_intake_note(client, seeded) -> None:
    assert client.post("/api/trace", json={"text": "please prioritize taxes"}).json() == {"status": "ok"}
    tip = _remote_tip(seeded.bare)
    assert tip.author.name == "haku-console"
    assert tip.message == "console: trace"
    notes = list((seeded.settings.clone_dir / "intake").glob("*-trace.md"))
    assert len(notes) == 1
    assert "please prioritize taxes" in notes[0].read_text()


if __name__ == "__main__":
    pytest_bazel.main()
