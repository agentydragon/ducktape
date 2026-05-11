"""HTTP-surface tests for the casino backend after the Y-CRDT removal."""

from __future__ import annotations

import sqlite3
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


def _grant_credits(client: TestClient, n: int, action_id: str = "seed-credits") -> None:
    """Earn `n` credits via /actions/session/add-past (seconds = n * 60)."""
    r = client.post(
        "/actions/session/add-past",
        json={"client_action_id": action_id, "subject": "Seed", "seconds": n * 60, "ended_at_ms": 1_700_000_000_000},
    )
    assert r.status_code == 200, r.text


def _grant_tokens(client: TestClient, n: int, action_prefix: str = "seed-tokens") -> None:
    _grant_credits(client, n, action_id=f"{action_prefix}-credits")
    r = client.post("/actions/convert", json={"client_action_id": f"{action_prefix}-convert", "amount": n})
    assert r.status_code == 200, r.text


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_me_returns_default_user_without_oidc(client: TestClient) -> None:
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json() == {"username": "default"}


def test_state_returns_seed_shape(client: TestClient) -> None:
    r = client.get("/state")
    assert r.status_code == 200
    state = r.json()
    assert state["balance"] == {"credits": 0, "tokens": 0}
    assert state["sessions"] == []
    assert state["prize_log"] == []
    assert len(state["prizes"]) == 6


def test_session_complete_inserts_row_and_grants_credits(client: TestClient) -> None:
    r = client.post(
        "/actions/session/complete",
        json={
            "client_action_id": "complete-1",
            "subject": "Biochem",
            "start_time_ms": 1_700_000_000_000,
            "paused_duration_ms": 0,
            "ended_at_ms": 1_700_000_000_000 + 25 * 60 * 1000,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["seconds"] == 25 * 60
    assert body["result"]["credits_earned"] == 25

    state = client.get("/state").json()
    assert state["balance"]["credits"] == 25
    assert len(state["sessions"]) == 1
    assert state["sessions"][0]["subject"] == "Biochem"


def test_session_complete_with_paused_duration_subtracts_pause_time(client: TestClient) -> None:
    """A 30-minute wall clock with a 5-minute pause should yield 25 minutes."""
    start = 1_700_000_000_000
    r = client.post(
        "/actions/session/complete",
        json={
            "client_action_id": "complete-paused",
            "subject": "Pharmacology",
            "start_time_ms": start,
            "paused_duration_ms": 5 * 60 * 1000,
            "ended_at_ms": start + 30 * 60 * 1000,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["seconds"] == 25 * 60


def test_session_complete_zero_seconds_writes_no_session_row(client: TestClient) -> None:
    """An accidental same-instant complete shouldn't pollute the sessions table."""
    r = client.post(
        "/actions/session/complete",
        json={
            "client_action_id": "complete-instant",
            "subject": "X",
            "start_time_ms": 1_700_000_000_000,
            "paused_duration_ms": 0,
            "ended_at_ms": 1_700_000_000_000,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["credits_earned"] == 0
    assert client.get("/state").json()["sessions"] == []


def test_session_complete_is_idempotent(client: TestClient) -> None:
    body = {
        "client_action_id": "complete-idem",
        "subject": "Anatomy",
        "start_time_ms": 1_700_000_000_000,
        "paused_duration_ms": 0,
        "ended_at_ms": 1_700_000_000_000 + 10 * 60 * 1000,
    }
    first = client.post("/actions/session/complete", json=body).json()
    second = client.post("/actions/session/complete", json=body).json()
    assert second["event"]["id"] == first["event"]["id"]
    assert client.get("/state").json()["balance"]["credits"] == 10


def test_session_edit_and_delete_adjust_credits(client: TestClient) -> None:
    _grant_credits(client, 30)
    state = client.get("/state").json()
    sid = state["sessions"][0]["id"]

    edit = client.post(
        "/actions/session/edit", json={"client_action_id": "edit-1", "session_id": sid, "seconds": 10 * 60}
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["result"]["credits_delta"] == -20
    assert client.get("/state").json()["balance"]["credits"] == 10

    delete = client.post("/actions/session/delete", json={"client_action_id": "del-1", "session_id": sid})
    assert delete.status_code == 200, delete.text
    assert client.get("/state").json()["sessions"] == []
    assert client.get("/state").json()["balance"]["credits"] == 0


def test_convert_credits_to_tokens(client: TestClient) -> None:
    _grant_credits(client, 10)
    r = client.post("/actions/convert", json={"client_action_id": "conv-1", "amount": 4})
    assert r.status_code == 200, r.text
    state = client.get("/state").json()
    assert state["balance"] == {"credits": 6, "tokens": 4}


def test_convert_insufficient_credits_returns_409(client: TestClient) -> None:
    r = client.post("/actions/convert", json={"client_action_id": "conv-bad", "amount": 5})
    assert r.status_code == 409
    assert r.json()["detail"]["rule"] == "insufficient_credits"


def test_prize_create_and_delete(client: TestClient) -> None:
    create = client.post("/actions/prize/create", json={"client_action_id": "prize-c-1", "name": "Mocha", "cost": 45})
    assert create.status_code == 200, create.text
    prize_id = create.json()["result"]["prize_id"]

    state = client.get("/state").json()
    assert any(p["id"] == prize_id and p["name"] == "Mocha" for p in state["prizes"])

    delete = client.post("/actions/prize/delete", json={"client_action_id": "prize-d-1", "prize_id": prize_id})
    assert delete.status_code == 200, delete.text
    state = client.get("/state").json()
    assert not any(p["id"] == prize_id for p in state["prizes"])


def test_prize_delete_unknown_returns_409(client: TestClient) -> None:
    r = client.post("/actions/prize/delete", json={"client_action_id": "del-missing", "prize_id": "nope"})
    assert r.status_code == 409
    assert r.json()["detail"]["rule"] == "prize"


def test_prize_redeem_writes_log_and_subtracts_tokens(client: TestClient) -> None:
    _grant_tokens(client, 100)

    r = client.post("/actions/prize/redeem", json={"client_action_id": "redeem-1", "prize_id": "p1"})
    assert r.status_code == 200, r.text

    state = client.get("/state").json()
    assert state["balance"]["tokens"] == 70  # p1 cost is 30
    assert len(state["prize_log"]) == 1
    assert state["prize_log"][0]["name"] == "Anime episode break"


def test_redeem_insufficient_tokens_returns_409(client: TestClient) -> None:
    r = client.post("/actions/prize/redeem", json={"client_action_id": "redeem-broke", "prize_id": "p1"})
    assert r.status_code == 409
    assert r.json()["detail"]["rule"] == "insufficient_tokens"


def test_slots_spin_writes_game_event(client: TestClient) -> None:
    _grant_credits(client, 5)
    r = client.post("/casino/slots/spin", json={"client_action_id": "slots-1", "wager_credits": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event"]["action_type"] == "casino.slots.spin"
    assert body["game_event"]["source"] == "server_resolved"
    assert body["game_event"]["rng_version"] == "server-secrets-v1"

    state = client.get("/state").json()
    assert state["balance"]["credits"] == 4


def test_blackjack_deal_creates_hand(client: TestClient) -> None:
    _grant_credits(client, 5)
    r = client.post("/casino/blackjack/deal", json={"client_action_id": "bj-deal-1", "wager_credits": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["hand_id"].startswith("bj-")
    assert body["result"]["phase"] in {"playing", "done"}


def test_import_replaces_state_and_writes_snapshot(client: TestClient) -> None:
    _grant_credits(client, 7)  # so there's something for the snapshot to capture

    r = client.post(
        "/actions/import",
        json={
            "client_action_id": "import-1",
            "data": {
                "credits": 0,
                "tokens": 5,
                "sessions": [{"id": "imp-1", "subject": "Imported", "seconds": 60, "ended_at_ms": 1700000000000}],
                "prizes": [{"id": "p-imp", "name": "Imported prize", "cost": 9}],
                "prize_log": [],
            },
        },
    )
    assert r.status_code == 200, r.text

    state = client.get("/state").json()
    assert state["balance"] == {"credits": 0, "tokens": 5}
    assert [s["id"] for s in state["sessions"]] == ["imp-1"]
    assert [p["id"] for p in state["prizes"]] == ["p-imp"]


def test_reset_zeroes_balance_keeps_prizes(client: TestClient) -> None:
    _grant_credits(client, 7)
    r = client.post("/actions/reset", json={"client_action_id": "reset-1"})
    assert r.status_code == 200, r.text
    state = client.get("/state").json()
    assert state["balance"] == {"credits": 0, "tokens": 0}
    assert state["sessions"] == []
    assert len(state["prizes"]) == 6


def test_pre_alembic_user_db_is_baselined_and_upgraded(tmp_path: Path) -> None:
    """A DB with the legacy schema (only `doc` table, no alembic_version) is
    baselined at 0001 and upgraded through 0004 on first request."""
    db_path = tmp_path / "casino-default.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE doc (id INTEGER PRIMARY KEY, update_blob BLOB NOT NULL)")
        # An empty Y.Doc encodes as `b"\x00\x00"` (zero clients, zero deletes);
        # alembic 0004 backfill produces an empty state from it.
        conn.execute("INSERT INTO doc (id, update_blob) VALUES (1, ?)", (b"\x00\x00",))

    app = create_app(Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "nonexistent_dist"))
    with TestClient(app) as c:
        r = c.get("/state")
        assert r.status_code == 200, r.text

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0004",)
        # After 0004, the legacy doc table is gone.
        assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'doc'").fetchone() is None


def test_users_have_isolated_state(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "nonexistent_dist"))
    dep = app.state.current_user_dep

    with TestClient(app) as client:
        app.dependency_overrides[dep] = lambda: "alice"
        _grant_credits(client, 50, action_id="alice-seed")
        assert client.get("/state").json()["balance"]["credits"] == 50

        app.dependency_overrides[dep] = lambda: "bob"
        assert client.get("/state").json()["balance"]["credits"] == 0

        assert (tmp_path / "casino-alice.db").exists()
        assert (tmp_path / "casino-bob.db").exists()


def test_ws_emits_state_changed_on_connect(client: TestClient) -> None:
    """The server pings every newly-connected tab so it does an initial /state fetch."""
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg == {"type": "state_changed"}


def test_ws_broadcasts_state_changed_after_action(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "nonexistent_dist")
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        # Drain bootstrap pings.
        ws1.receive_json()
        ws2.receive_json()

        client.post(
            "/actions/session/add-past",
            json={
                "client_action_id": "broadcast-1",
                "subject": "Test",
                "seconds": 60,
                "ended_at_ms": 1_700_000_000_000,
            },
        )

        for ws in (ws1, ws2):
            msg = ws.receive_json()
            assert msg == {"type": "state_changed"}


if __name__ == "__main__":
    pytest_bazel.main()
