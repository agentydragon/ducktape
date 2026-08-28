"""HTTP-surface tests for the casino backend."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import pytest_bazel
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from study_casino.app import create_app
from study_casino.changelog import CHANGELOG
from study_casino.config import Settings
from study_casino.games import RNG_VERSION, draw_cards, load_cards, make_shoe, spin_roulette
from study_casino.models import BlackjackHandRow, RngActionAuditRow, RngCallAuditRow
from study_casino.rng import AuditedRandom

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

_TEST_RNG_SECRET = "test-auditable-rng-secret-with-enough-bytes"

# Mid-afternoon Pacific (2023-11-14); stepping by whole days never crosses a
# Pacific-midnight boundary within a session.
_BASE_MS = 1_700_000_000_000


def _settings(tmp_path: Path, db_url: str, **kwargs: object) -> Settings:
    return Settings(
        database_url=db_url, frontend_dist_dir=tmp_path / "nonexistent_dist", rng_secret=_TEST_RNG_SECRET, **kwargs
    )


@pytest.fixture
def client(tmp_path: Path, db_url: str) -> TestClient:
    """Default unauth client. The fallback user `default` is configured as
    an admin so existing prize-create/-delete tests still pass; non-admin
    behaviour is exercised via a separate fixture below."""
    return TestClient(create_app(_settings(tmp_path, db_url, admin_users={"default"})))


@pytest.fixture
def non_admin_client(tmp_path: Path, db_url: str) -> TestClient:
    """Client where `default` is not an admin — used to verify 403 paths."""
    return TestClient(create_app(_settings(tmp_path, db_url)))


@pytest.fixture
def admin_app(tmp_path: Path, db_url: str) -> Iterator[tuple[TestClient, Callable[[str], None]]]:
    """App where `rai` is the sole admin; yields `(client, set_user)` where
    `set_user(username)` switches the authenticated user via a dependency
    override."""
    app = create_app(_settings(tmp_path, db_url, admin_users={"rai"}))
    dep = app.state.current_user_dep

    def set_user(username: str) -> None:
        app.dependency_overrides[dep] = lambda: username

    with TestClient(app) as client:
        yield client, set_user


def _grant_credits(client: TestClient, n: int, action_id: str = "seed-credits") -> None:
    """Earn `n` credits via /actions/session/add-past (seconds = n * 60)."""
    r = client.post(
        "/actions/session/add-past",
        json={"client_action_id": action_id, "subject": "Seed", "seconds": n * 60, "ended_at_ms": _BASE_MS},
    )
    assert r.status_code == 200, r.text


def _grant_tokens(client: TestClient, n: int, action_prefix: str = "seed-tokens") -> None:
    _grant_credits(client, n, action_id=f"{action_prefix}-credits")
    r = client.post("/actions/convert", json={"client_action_id": f"{action_prefix}-convert", "amount": n})
    assert r.status_code == 200, r.text


def _rng_audit_rows(db_url: str, client_action_id: str) -> tuple[RngActionAuditRow | None, list[RngCallAuditRow]]:
    engine = create_engine(db_url)
    with Session(engine) as s:
        action = s.scalar(
            select(RngActionAuditRow).where(
                RngActionAuditRow.user_id == "default", RngActionAuditRow.client_action_id == client_action_id
            )
        )
        calls = (
            list(
                s.scalars(
                    select(RngCallAuditRow)
                    .where(RngCallAuditRow.user_id == "default", RngCallAuditRow.client_action_id == client_action_id)
                    .order_by(RngCallAuditRow.call_index)
                ).all()
            )
            if action is not None
            else []
        )
    engine.dispose()
    return action, calls


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_deployment_reports_commit_from_runtime_image_tag(
    tmp_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDY_CASINO_IMAGE_TAG", "devel-20260521192052-4ab4c77")
    with TestClient(create_app(_settings(tmp_path, db_url))) as c:
        r = c.get("/deployment")
    assert r.status_code == 200
    assert r.json() == {
        "image_tag": "devel-20260521192052-4ab4c77",
        "source_commit": "4ab4c77",
        "source_commit_url": "https://github.com/agentydragon/ducktape/commit/4ab4c77",
    }


def test_index_html_bypasses_conditional_static_cache(tmp_path: Path, db_url: str) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    settings = Settings(database_url=db_url, frontend_dist_dir=dist, rng_secret=_TEST_RNG_SECRET)
    with TestClient(create_app(settings)) as c:
        r = c.get("/", headers={"If-None-Match": '"stale"', "If-Modified-Since": "Sun, 01 Jan 2099 00:00:00 GMT"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "etag" not in r.headers
    assert "last-modified" not in r.headers
    assert r.text == "<!doctype html><div id='root'></div>"


def test_me_returns_default_user_without_oidc(client: TestClient) -> None:
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json() == {"username": "default", "is_admin": True}


def test_state_returns_seed_shape(client: TestClient) -> None:
    r = client.get("/state")
    assert r.status_code == 200
    state = r.json()
    assert state["balance"] == {"credits_millis": 0, "tokens": 0}
    assert state["credit_state"] == {
        "streak_days": 0,
        "streak_bonus_percent": 0,
        "rest_days_available": 0,
        "daily_bonus_claimed_today": False,
        "today_study_seconds": 0,
        "daily_bonus_threshold_seconds": 300,
        "daily_bonus_credits": 30,
        "pending_bonus_percent": 1,
    }
    assert state["sessions"] == []
    assert state["prize_log"] == []
    assert len(state["prizes"]) == 6
    # A fresh user has every changelog entry unacked.
    assert [entry["id"] for entry in state["changelog_unacked"]] == [entry.id for entry in CHANGELOG]


def test_changelog_ack_advances_cursor(client: TestClient) -> None:
    latest = client.get("/state").json()["changelog_unacked"][-1]["id"]
    r = client.post("/actions/changelog/ack", json={"client_action_id": "clog-1", "last_id": latest})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["acked_through"] == latest
    assert client.get("/state").json()["changelog_unacked"] == []

    # Acking an already-acked (older) id never rewinds the cursor.
    r = client.post("/actions/changelog/ack", json={"client_action_id": "clog-2", "last_id": latest})
    assert r.json()["result"]["acked_through"] == latest
    assert client.get("/state").json()["changelog_unacked"] == []


def test_changelog_ack_unknown_id_returns_409(client: TestClient) -> None:
    r = client.post("/actions/changelog/ack", json={"client_action_id": "clog-bad", "last_id": 999})
    assert r.status_code == 409
    assert r.json()["detail"]["rule"] == "changelog"


def test_session_complete_inserts_row_and_grants_credits(client: TestClient) -> None:
    r = client.post(
        "/actions/session/complete",
        json={
            "client_action_id": "complete-1",
            "subject": "Biochem",
            "start_time_ms": _BASE_MS,
            "paused_duration_ms": 0,
            "ended_at_ms": _BASE_MS + 25 * 60 * 1000,
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["seconds"] == 25 * 60
    # First live session of the day: streak day 1 (+1%) + 30-credit daily
    # bonus → (25 + 30) × 1.01 = 55.55 credits = 55550 millis.
    assert result["streak_days"] == 1
    assert result["streak_bonus_percent"] == 1
    assert result["daily_bonus_millis"] == 30300
    assert result["credits_earned_millis"] == 55550

    state = client.get("/state").json()
    assert state["balance"]["credits_millis"] == 55550
    assert state["credit_state"]["streak_days"] == 1
    assert len(state["sessions"]) == 1
    assert state["sessions"][0]["subject"] == "Biochem"


def test_session_complete_with_paused_duration_subtracts_pause_time(client: TestClient) -> None:
    """A 30-minute wall clock with a 5-minute pause should yield 25 minutes."""
    start = _BASE_MS
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
            "start_time_ms": _BASE_MS,
            "paused_duration_ms": 0,
            "ended_at_ms": _BASE_MS,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["credits_earned_millis"] == 0
    assert client.get("/state").json()["sessions"] == []


def test_session_complete_is_idempotent(client: TestClient) -> None:
    body = {
        "client_action_id": "complete-idem",
        "subject": "Anatomy",
        "start_time_ms": _BASE_MS,
        "paused_duration_ms": 0,
        "ended_at_ms": _BASE_MS + 10 * 60 * 1000,
    }
    first = client.post("/actions/session/complete", json=body).json()
    second = client.post("/actions/session/complete", json=body).json()
    assert second["event"]["id"] == first["event"]["id"]
    # Awarded once: (10 + 30 daily bonus) × 1.01 — the retry must not re-award.
    assert client.get("/state").json()["balance"]["credits_millis"] == 40400


def test_session_edit_and_delete_adjust_credits(client: TestClient) -> None:
    _grant_credits(client, 30)
    state = client.get("/state").json()
    sid = state["sessions"][0]["id"]

    edit = client.post(
        "/actions/session/edit", json={"client_action_id": "edit-1", "session_id": sid, "seconds": 10 * 60}
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["result"]["credits_delta_millis"] == -20000
    assert client.get("/state").json()["balance"]["credits_millis"] == 10000

    delete = client.post("/actions/session/delete", json={"client_action_id": "del-1", "session_id": sid})
    assert delete.status_code == 200, delete.text
    assert client.get("/state").json()["sessions"] == []
    assert client.get("/state").json()["balance"]["credits_millis"] == 0


_DAY_MS = 24 * 3600 * 1000


def _complete_session(client: TestClient, action_id: str, ended_at_ms: int, minutes: int = 10) -> dict:
    r = client.post(
        "/actions/session/complete",
        json={
            "client_action_id": action_id,
            "subject": "Streak",
            "start_time_ms": ended_at_ms - minutes * 60 * 1000,
            "paused_duration_ms": 0,
            "ended_at_ms": ended_at_ms,
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert isinstance(result, dict)
    return result


def test_streak_grows_on_consecutive_days_and_multiplies_awards(client: TestClient) -> None:
    day1 = _complete_session(client, "streak-d1", _BASE_MS)
    assert day1["streak_days"] == 1
    assert day1["credits_earned_millis"] == 40400  # (10 + 30 bonus) × 1.01
    day2 = _complete_session(client, "streak-d2", _BASE_MS + _DAY_MS)
    assert day2["streak_days"] == 2
    assert day2["credits_earned_millis"] == 40800  # (10 + 30 bonus) × 1.02


def test_daily_bonus_fires_once_per_day(client: TestClient) -> None:
    first = _complete_session(client, "bonus-1", _BASE_MS)
    assert first["daily_bonus_millis"] == 30300
    second = _complete_session(client, "bonus-2", _BASE_MS + 3_600_000)
    assert second["daily_bonus_millis"] == 0
    assert second["credits_earned_millis"] == 10100  # 10 × 1.01, no second bonus


def test_streak_resets_after_gap_without_rest_days(client: TestClient) -> None:
    _complete_session(client, "gap-d1", _BASE_MS)
    day4 = _complete_session(client, "gap-d4", _BASE_MS + 3 * _DAY_MS)
    assert day4["streak_days"] == 1
    assert day4["streak_bonus_percent"] == 1


def test_sub_threshold_session_earns_fractional_credits_without_streak(client: TestClient) -> None:
    result = _complete_session(client, "tiny-1", _BASE_MS, minutes=4)
    assert result["streak_days"] == 0
    assert result["daily_bonus_millis"] == 0
    assert result["credits_earned_millis"] == 4000
    assert client.get("/state").json()["credit_state"]["streak_days"] == 0


def test_state_reflects_todays_live_session(client: TestClient) -> None:
    """A session completed 'now' shows up in today's credit_state: bonus
    claimed, today's seconds counted, and pending == current percent."""
    _complete_session(client, "today-live", int(time.time() * 1000))
    cs = client.get("/state").json()["credit_state"]
    assert cs["daily_bonus_claimed_today"] is True
    assert cs["today_study_seconds"] == 10 * 60
    assert cs["streak_days"] == 1
    assert cs["streak_bonus_percent"] == 1
    assert cs["pending_bonus_percent"] == 1  # today already qualified — nothing further pending


def test_add_past_session_earns_credits_without_streak_effects(client: TestClient) -> None:
    _grant_credits(client, 30)  # 30-minute backfill — over the daily threshold
    state = client.get("/state").json()
    assert state["balance"]["credits_millis"] == 30000
    assert state["credit_state"]["streak_days"] == 0
    assert state["credit_state"]["daily_bonus_claimed_today"] is False


def test_convert_credits_to_tokens(client: TestClient) -> None:
    _grant_credits(client, 10)
    r = client.post("/actions/convert", json={"client_action_id": "conv-1", "amount": 4})
    assert r.status_code == 200, r.text
    state = client.get("/state").json()
    assert state["balance"] == {"credits_millis": 6000, "tokens": 4}


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
    assert state["balance"]["tokens"] == 64  # p1 cost is 36
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
    assert body["game_event"]["rng_version"] == RNG_VERSION

    state = client.get("/state").json()
    assert state["balance"]["credits_millis"] == 4000


def test_roulette_spin_writes_replayable_rng_audit(tmp_path: Path, db_url: str) -> None:
    client = TestClient(create_app(_settings(tmp_path, db_url, admin_users={"default"})))
    _grant_credits(client, 5)

    request = {"client_action_id": "roulette-audit", "wager_credits": 1, "bet_type": "red", "bet_number": None}
    r = client.post("/casino/roulette/spin", json=request)
    assert r.status_code == 200, r.text
    result = r.json()["result"]

    action, calls = _rng_audit_rows(db_url, "roulette-audit")
    assert action is not None
    assert action.rng_version == RNG_VERSION
    assert action.rng_key_id == "study-casino-rng-v1"
    assert json.loads(action.seed_material_json)["request_body"] == request
    assert action.seed_digest_hex != _TEST_RNG_SECRET
    assert len(calls) == 1
    assert calls[0].purpose == "roulette.wheel_index"
    assert json.loads(calls[0].result_json)["value"] == result["result_index"]

    replay = AuditedRandom.from_seed_material_json(
        secret=_TEST_RNG_SECRET.encode(),
        rng_version=action.rng_version,
        rng_key_id=action.rng_key_id,
        seed_material_json=action.seed_material_json,
    )
    replayed = spin_roulette(1, "red", None, replay)
    assert replayed.outcome.result_index == result["result_index"]
    assert replayed.outcome.result_number == result["result_number"]


def test_slots_rng_audit_records_weighted_draws_and_retry_is_idempotent(tmp_path: Path, db_url: str) -> None:
    client = TestClient(create_app(_settings(tmp_path, db_url, admin_users={"default"})))
    _grant_credits(client, 5)

    request = {"client_action_id": "slots-audit", "wager_credits": 1}
    first = client.post("/casino/slots/spin", json=request)
    second = client.post("/casino/slots/spin", json=request)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["event"]["id"] == first.json()["event"]["id"]

    action, calls = _rng_audit_rows(db_url, "slots-audit")
    assert action is not None
    assert len(calls) == 3
    assert [call.purpose for call in calls] == ["slots.reel.0", "slots.reel.1", "slots.reel.2"]
    assert [json.loads(call.result_json)["item_id"] for call in calls] == first.json()["result"]["symbols"]

    rejected = client.post("/casino/slots/spin", json={"client_action_id": "slots-rejected", "wager_credits": 999})
    assert rejected.status_code == 409
    rejected_action, rejected_calls = _rng_audit_rows(db_url, "slots-rejected")
    assert rejected_action is None
    assert rejected_calls == []


def test_blackjack_deal_rng_audit_replays_stored_shoe(tmp_path: Path, db_url: str) -> None:
    client = TestClient(create_app(_settings(tmp_path, db_url, admin_users={"default"})))
    _grant_credits(client, 5)

    request = {"client_action_id": "bj-audit", "wager_credits": 1}
    r = client.post("/casino/blackjack/deal", json=request)
    assert r.status_code == 200, r.text
    hand_id = r.json()["result"]["hand_id"]

    action, calls = _rng_audit_rows(db_url, "bj-audit")
    assert action is not None
    assert len(calls) == 4 * 52 - 1
    assert calls[0].method == "shuffle_swap"

    replay = AuditedRandom.from_seed_material_json(
        secret=_TEST_RNG_SECRET.encode(),
        rng_version=action.rng_version,
        rng_key_id=action.rng_key_id,
        seed_material_json=action.seed_material_json,
    )
    shoe = make_shoe(replay)
    p1, shoe = draw_cards(shoe, 1)
    d1, shoe = draw_cards(shoe, 1)
    p2, shoe = draw_cards(shoe, 1)
    d2, shoe = draw_cards(shoe, 1)

    engine = create_engine(db_url)
    with Session(engine) as s:
        row = s.get(BlackjackHandRow, ("default", hand_id))
        assert row is not None
        assert load_cards(row.shoe_json) == shoe
        assert load_cards(row.player_json) == [*p1, *p2]
        assert load_cards(row.dealer_json) == [*d1, *d2]
    engine.dispose()


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
                "sessions": [{"id": "imp-1", "subject": "Imported", "seconds": 60, "ended_at_ms": _BASE_MS}],
                "prizes": [{"id": "p-imp", "name": "Imported prize", "cost": 9}],
                "prize_log": [],
            },
        },
    )
    assert r.status_code == 200, r.text

    state = client.get("/state").json()
    assert state["balance"] == {"credits_millis": 0, "tokens": 5}
    assert [s["id"] for s in state["sessions"]] == ["imp-1"]
    assert [p["id"] for p in state["prizes"]] == ["p-imp"]


def test_reset_zeroes_balance_keeps_prizes(client: TestClient) -> None:
    _grant_credits(client, 7)
    r = client.post("/actions/reset", json={"client_action_id": "reset-1"})
    assert r.status_code == 200, r.text
    state = client.get("/state").json()
    assert state["balance"] == {"credits_millis": 0, "tokens": 0}
    assert state["sessions"] == []
    assert len(state["prizes"]) == 6


def test_users_have_isolated_state(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    """Two users hitting the same shared-schema store see only their own balances."""
    client, set_user = admin_app

    set_user("alice")
    _grant_credits(client, 50, action_id="alice-seed")
    assert client.get("/state").json()["balance"]["credits_millis"] == 50000

    set_user("bob")
    assert client.get("/state").json()["balance"]["credits_millis"] == 0
    _grant_credits(client, 7, action_id="bob-seed")
    assert client.get("/state").json()["balance"]["credits_millis"] == 7000

    # Alice's balance is unchanged by bob's activity.
    set_user("alice")
    assert client.get("/state").json()["balance"]["credits_millis"] == 50000


def test_ws_emits_state_changed_on_connect(client: TestClient) -> None:
    """The server pings every newly-connected tab so it does an initial /state fetch."""
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg == {"type": "state_changed"}


def test_ws_broadcasts_state_changed_after_action(tmp_path: Path, db_url: str) -> None:
    app = create_app(_settings(tmp_path, db_url))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        # Drain bootstrap pings.
        ws1.receive_json()
        ws2.receive_json()

        client.post(
            "/actions/session/add-past",
            json={"client_action_id": "broadcast-1", "subject": "Test", "seconds": 60, "ended_at_ms": _BASE_MS},
        )

        for ws in (ws1, ws2):
            msg = ws.receive_json()
            assert msg == {"type": "state_changed"}


# ── Admin-only prize management ──────────────────────────────────────────────


def test_me_reports_admin_flag(client: TestClient, non_admin_client: TestClient) -> None:
    assert client.get("/me").json() == {"username": "default", "is_admin": True}
    assert non_admin_client.get("/me").json() == {"username": "default", "is_admin": False}


def test_non_admin_cannot_create_prize_for_self(non_admin_client: TestClient) -> None:
    r = non_admin_client.post(
        "/actions/prize/create", json={"client_action_id": "noadm-c", "name": "Mocha", "cost": 45}
    )
    assert r.status_code == 403


def test_non_admin_cannot_delete_prize(non_admin_client: TestClient) -> None:
    r = non_admin_client.post("/actions/prize/delete", json={"client_action_id": "noadm-d", "prize_id": "p1"})
    assert r.status_code == 403


def test_non_admin_can_still_redeem_prize(non_admin_client: TestClient) -> None:
    """Auragon can still redeem prizes Rai created."""
    _grant_tokens(non_admin_client, 100)
    r = non_admin_client.post("/actions/prize/redeem", json={"client_action_id": "noadm-r", "prize_id": "p1"})
    assert r.status_code == 200, r.text


def test_admin_can_create_prize_for_other_user(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    """Rai (admin) creates a prize in Auragon's catalog."""
    c, set_user = admin_app
    set_user("rai")
    r = c.post(
        "/actions/prize/create",
        json={"client_action_id": "rai-add-1", "name": "Custom prize for auragon", "cost": 7, "target_user": "auragon"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["user"] == "auragon"

    # Auragon sees the new prize in her catalog.
    set_user("auragon")
    state = c.get("/state").json()
    assert any(p["name"] == "Custom prize for auragon" for p in state["prizes"])

    # And Rai's own catalog is untouched.
    set_user("rai")
    rai_state = c.get("/state").json()
    assert not any(p["name"] == "Custom prize for auragon" for p in rai_state["prizes"])


def test_admin_users_endpoint_lists_seeded_users(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    c, set_user = admin_app
    # Seed two users by calling /state on each.
    for u in ("auragon", "rai"):
        set_user(u)
        c.get("/state")

    set_user("rai")
    r = c.get("/admin/users")
    assert r.status_code == 200, r.text
    assert set(r.json()["users"]) >= {"auragon", "rai"}


def test_non_admin_admin_endpoints_return_403(non_admin_client: TestClient) -> None:
    assert non_admin_client.get("/admin/users").status_code == 403
    assert non_admin_client.get("/admin/state?user=default").status_code == 403


def test_admin_state_returns_target_user_state(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    c, set_user = admin_app
    set_user("auragon")
    _grant_credits(c, 12, action_id="auragon-seed")

    set_user("rai")
    r = c.get("/admin/state", params={"user": "auragon"})
    assert r.status_code == 200, r.text
    assert r.json()["balance"]["credits_millis"] == 12000


def test_admin_state_unknown_user_returns_404_without_seeding(
    admin_app: tuple[TestClient, Callable[[str], None]],
) -> None:
    """A typo in ?user= must 404, NOT lazy-seed a brand-new user."""
    c, set_user = admin_app
    set_user("rai")
    # 'rai' must exist for /admin/users to be non-empty later.
    c.get("/state")

    assert c.get("/admin/state", params={"user": "ghost"}).status_code == 404

    # The 404 path must not have seeded 'ghost'.
    assert "ghost" not in c.get("/admin/users").json()["users"]


def test_admin_state_rejects_overlong_user_param(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    c, set_user = admin_app
    set_user("rai")
    # 65 chars — one over the user_id String(64) column.
    assert c.get("/admin/state", params={"user": "x" * 65}).status_code == 422
    assert c.get("/admin/state", params={"user": ""}).status_code == 422


# ── Casino stats endpoint ────────────────────────────────────────────────────


def test_casino_stats_returns_empty_for_fresh_user(client: TestClient) -> None:
    r = client.get("/casino/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_count"] == 0
    assert body["since_date"] == "2026-05-07"
    assert {g["game"] for g in body["games"]} == {"roulette", "blackjack", "slots"}


def test_casino_stats_counts_a_real_spin(client: TestClient) -> None:
    _grant_credits(client, 5)
    r = client.post("/casino/slots/spin", json={"client_action_id": "stats-spin", "wager_credits": 1})
    assert r.status_code == 200, r.text

    body = client.get("/casino/stats").json()
    slots = next(g for g in body["games"] if g["game"] == "slots")
    assert slots["total"]["count"] == 1
    assert slots["total"]["wagered"] == 1
    assert len(slots["timeline"]) == 1
    assert slots["timeline"][0]["count"] == 1


def test_admin_casino_stats_returns_target_user_stats(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    c, set_user = admin_app
    set_user("auragon")
    _grant_credits(c, 5, action_id="auragon-stats-seed")
    r = c.post("/casino/slots/spin", json={"client_action_id": "auragon-spin", "wager_credits": 1})
    assert r.status_code == 200, r.text

    set_user("rai")
    r = c.get("/admin/casino/stats", params={"user": "auragon"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "auragon"
    assert body["event_count"] == 1


def test_admin_casino_stats_404_for_unknown_user(admin_app: tuple[TestClient, Callable[[str], None]]) -> None:
    c, set_user = admin_app
    set_user("rai")
    c.get("/state")  # seed 'rai' so /admin/users isn't empty
    r = c.get("/admin/casino/stats", params={"user": "ghost"})
    assert r.status_code == 404


def test_admin_casino_stats_403_for_non_admin(non_admin_client: TestClient) -> None:
    assert non_admin_client.get("/admin/casino/stats?user=default").status_code == 403


if __name__ == "__main__":
    pytest_bazel.main()
