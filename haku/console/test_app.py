"""Integration test: the FastAPI app over a seeded local haku-state clone.

The write endpoints are exercised against a real (local, bare) git remote, so each
test asserts the commit actually landed on the remote with the ``haku-console`` identity
— the console's whole job is to turn an operator click into a git event Haku can reduce.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest_bazel
from fastapi.testclient import TestClient

from haku.console.app import create_app


def _remote_tip(bare: Path) -> pygit2.Commit:
    repo = pygit2.Repository(str(bare))
    return repo[repo.lookup_reference("refs/heads/main").target].peel(pygit2.Commit)


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


def test_click_records_overlay_and_renders_clicked(seeded) -> None:
    app = create_app(seeded.settings, git_state=seeded.git_state)
    item_id = seeded.ids[0]
    with TestClient(app) as client:
        resp = client.post(f"/items/{item_id}/actions/snooze")  # follows the 303 back to /
        assert resp.status_code == 200
        assert f'action="/items/{item_id}/actions/snooze/unclick"' in client.get("/").text
    assert (item_id, "snooze") in seeded.git_state.read_clicks()
    tip = _remote_tip(seeded.bare)
    assert tip.author.name == "haku-console"
    assert tip.message == f"console: click snooze on {item_id}"


def test_unclick_retracts_the_overlay(seeded) -> None:
    app = create_app(seeded.settings, git_state=seeded.git_state)
    item_id = seeded.ids[0]
    with TestClient(app) as client:
        client.post(f"/items/{item_id}/actions/snooze")
        client.post(f"/items/{item_id}/actions/snooze/unclick")
        page = client.get("/").text
        assert f'action="/items/{item_id}/actions/snooze"' in page  # back to a plain click
        assert "/unclick" not in page
    assert seeded.git_state.read_clicks() == set()
    assert _remote_tip(seeded.bare).message == f"console: unclick snooze on {item_id}"


def test_delete_clears_the_click(seeded) -> None:
    app = create_app(seeded.settings, git_state=seeded.git_state)
    item_id = seeded.ids[0]
    with TestClient(app) as client:
        client.post(f"/items/{item_id}/actions/snooze")
        assert client.delete(f"/items/{item_id}/actions/snooze").json() == {"status": "cleared"}
    assert seeded.git_state.read_clicks() == set()


def test_feedback_appends_intake_note(seeded) -> None:
    app = create_app(seeded.settings, git_state=seeded.git_state)
    with TestClient(app) as client:
        assert client.post("/feedback", data={"text": "please prioritize taxes"}).status_code == 200
    tip = _remote_tip(seeded.bare)
    assert tip.author.name == "haku-console"
    assert tip.message == "console: feedback"
    notes = list((seeded.settings.clone_dir / "intake").glob("*-feedback.md"))
    assert len(notes) == 1
    assert "please prioritize taxes" in notes[0].read_text()


if __name__ == "__main__":
    pytest_bazel.main()
