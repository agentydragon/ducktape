"""Integration test: the FastAPI JSON API over a seeded local haku-state clone.

The write endpoints are exercised against a real (local, bare) git remote, so each
test asserts the commit actually landed on the remote with the ``haku-console`` identity
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


def test_dashboard_lists_seeded_items(client, seeded) -> None:
    data = client.get("/api/dashboard").json()
    assert [it["title"] for it in data["items"]] == seeded.titles
    assert data["clicks"] == []


def test_click_records_overlay(client, seeded) -> None:
    item_id = seeded.ids[0]
    assert client.post(f"/api/items/{item_id}/actions/snooze").json() == {"status": "clicked"}
    assert {"item_id": item_id, "action_id": "snooze"} in client.get("/api/dashboard").json()["clicks"]
    assert (item_id, "snooze") in seeded.git_state.read_clicks()
    tip = _remote_tip(seeded.bare)
    assert tip.author.name == "haku-console"
    assert tip.message == f"console: click snooze on {item_id}"


def test_unclick_retracts_overlay(client, seeded) -> None:
    item_id = seeded.ids[0]
    client.post(f"/api/items/{item_id}/actions/snooze")
    assert client.delete(f"/api/items/{item_id}/actions/snooze").json() == {"status": "cleared"}
    assert seeded.git_state.read_clicks() == set()
    assert client.get("/api/dashboard").json()["clicks"] == []
    assert _remote_tip(seeded.bare).message == f"console: unclick snooze on {item_id}"


def test_feedback_appends_intake_note(client, seeded) -> None:
    assert client.post("/api/feedback", json={"text": "please prioritize taxes"}).json() == {"status": "ok"}
    tip = _remote_tip(seeded.bare)
    assert tip.author.name == "haku-console"
    assert tip.message == "console: feedback"
    notes = list((seeded.settings.clone_dir / "intake").glob("*-feedback.md"))
    assert len(notes) == 1
    assert "please prioritize taxes" in notes[0].read_text()


def test_item_feedback_appends_tagged_intake_note(client, seeded) -> None:
    item_id = seeded.ids[0]
    assert client.post("/api/feedback", json={"text": "this one is urgent", "item_id": item_id}).json() == {
        "status": "ok"
    }
    tip = _remote_tip(seeded.bare)
    assert tip.author.name == "haku-console"
    assert tip.message == f"console: feedback on {item_id}"
    # The note references the item id so Haku reduces it as feedback on that item.
    notes = list((seeded.settings.clone_dir / "intake").glob(f"*-feedback-{item_id}.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "this one is urgent" in body
    assert item_id in body


if __name__ == "__main__":
    pytest_bazel.main()
