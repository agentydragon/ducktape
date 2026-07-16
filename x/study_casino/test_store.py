"""SqlStore: state-dump, idempotent server actions, snapshot semantics, tenant isolation."""

from __future__ import annotations

import json

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from x.study_casino.actions import ConvertResult, ImportData, ImportPrize, ImportResult, ResetResult
from x.study_casino.models import Game, GameEventRow, GameEventSource, StateSnapshotRow
from x.study_casino.state import BalanceRead
from x.study_casino.store import ActionMutation, ActionRejectedError, ServerActionResult, SqlStore, locked_balance


@pytest.fixture
def store(db_url: str) -> SqlStore:
    return SqlStore(db_url)


# Single-tenant tests use "u" as the canonical test user.
_U = "u"


def _roulette_outcome(
    bet_type: str, won: bool, *, bet_number: int | None = None, multiplier: int = 2
) -> dict[str, object]:
    """Minimal RouletteOutcome dict — only `bet_type` and `won` matter for these tests."""
    return {
        "bet_type": bet_type,
        "bet_number": bet_number,
        "multiplier": multiplier,
        "result_color": "red",
        "result_number": 1,
        "result_index": 0,
        "won": won,
    }


def _slots_outcome(payout_kind: str) -> dict[str, object]:
    """Minimal SlotsOutcome dict — only `payout_kind` matters for these tests."""
    return {"symbols": ["a", "b", "c"], "glyphs": ["A", "B", "C"], "label": "test", "payout_kind": payout_kind}


def _blackjack_outcome(
    outcome: str, *, doubled: bool = False, dealer_cards: list[dict[str, str]] | None = None
) -> dict[str, object]:
    """Minimal BlackjackOutcome dict — only `outcome`, `doubled`, `dealer_cards` matter for these tests.

    `dealer_cards` defaults to a single 7♥ upcard so tests that don't care about
    upcard bucketing land in a non-aggregating slice.
    """
    return {
        "outcome": outcome,
        "text": "",
        "player_cards": [],
        "dealer_cards": dealer_cards if dealer_cards is not None else [{"rank": "7", "suit": "♥"}],
        "player_value": 0,
        "dealer_value": 0,
        "player_blackjack": False,
        "dealer_blackjack": False,
        "initial_wager": 1,
        "doubled": doubled,
    }


def _game_event(
    client_event_id: str,
    game: Game,
    outcome: dict[str, object],
    *,
    wager: int,
    payout: int,
    source: GameEventSource = "server_resolved",
    occurred_at_ms: int = 1_778_200_000_000,
) -> GameEventRow:
    """GameEventRow with the constant audit columns filled in; only the
    per-event fields vary across tests."""
    return GameEventRow(
        user_id=_U,
        client_event_id=client_event_id,
        server_at_ms=occurred_at_ms,
        occurred_at_ms=occurred_at_ms,
        game=game,
        event_type="settle",
        source=source,
        wager_credits=wager,
        payout_tokens=payout,
        credits_before=0,
        credits_after=0,
        tokens_before=0,
        tokens_after=0,
        server_credits=0,
        server_tokens=0,
        outcome_json=json.dumps(outcome),
        rules_version="server-rules-v1",
        rng_version="server-secrets-v1",
    )


def _seed_game_events(store: SqlStore, rows: list[GameEventRow]) -> None:
    store.state_dump(_U)  # seed the user's balance row first
    with store._Session() as s, s.begin():
        s.add_all(rows)


def test_fresh_store_has_zero_balance_and_default_prizes(store: SqlStore) -> None:
    state = store.state_dump(_U)
    assert state.balance == BalanceRead(credits_millis=0, tokens=0)
    assert state.sessions == []
    assert state.prize_log == []
    assert len(state.prizes) == 6  # DEFAULT_PRIZES


def test_run_server_action_persists_balance_and_writes_ledger(store: SqlStore) -> None:
    def grant_credits(s, _now_ms):
        balance = locked_balance(s, _U)
        balance.credits += 7000  # 7 credits, in millis
        return ActionMutation(result=ConvertResult(amount=7), details={"reason": "test"})

    result = store.run_server_action(
        username=_U, client_action_id="act-1", action_type="test.grant", mutator=grant_credits
    )
    assert isinstance(result, ServerActionResult)
    assert result.event.action_type == "test.grant"
    assert result.event.credits_before_millis == 0
    assert result.event.credits_after_millis == 7000
    assert result.result == ConvertResult(amount=7)

    state = store.state_dump(_U)
    assert state.balance.credits_millis == 7000

    ledger = store.list_ledger_events(_U)
    assert len(ledger) == 1
    assert ledger[0].client_action_id == "act-1"


def test_run_server_action_is_idempotent_on_retry(store: SqlStore) -> None:
    def grant_credits(s, _now_ms):
        balance = locked_balance(s, _U)
        balance.credits += 5000
        return ActionMutation(result=ConvertResult(amount=5))

    first = store.run_server_action(
        username=_U, client_action_id="act-dup", action_type="test.grant", mutator=grant_credits
    )
    second = store.run_server_action(
        username=_U, client_action_id="act-dup", action_type="test.grant", mutator=grant_credits
    )
    assert second.event.id == first.event.id
    assert second.result == first.result
    # Mutator was NOT replayed — credits stayed at 5000 millis, not 10000.
    assert store.state_dump(_U).balance.credits_millis == 5000


def test_action_rejected_rolls_back_mutator_changes(store: SqlStore) -> None:
    def half_then_reject(s, _now_ms):
        balance = locked_balance(s, _U)
        balance.credits += 99
        raise ActionRejectedError("nope", "no good")

    with pytest.raises(ActionRejectedError):
        store.run_server_action(
            username=_U, client_action_id="act-reject", action_type="test.reject", mutator=half_then_reject
        )

    # Transaction was rolled back: balance unchanged, no ledger row.
    assert store.state_dump(_U).balance.credits_millis == 0
    assert len(store.list_ledger_events(_U)) == 0


def test_snapshot_reason_writes_state_snapshots_row(store: SqlStore) -> None:
    def import_payload(s, _now_ms):
        store.replace_state_for_import(
            s,
            _U,
            ImportData(
                credits=11, tokens=22, sessions=[], prizes=[ImportPrize(id="p-only", name="Only", cost=5)], prize_log=[]
            ),
        )
        return ActionMutation(result=ImportResult(imported=True))

    store.run_server_action(
        username=_U,
        client_action_id="act-import",
        action_type="data.import",
        mutator=import_payload,
        snapshot_reason="before_import",
        snapshot_note="unit test",
    )

    state = store.state_dump(_U)
    # ImportData.credits is decimal credits; stored as millis.
    assert state.balance == BalanceRead(credits_millis=11000, tokens=22)
    assert [p.id for p in state.prizes] == ["p-only"]

    with store._Session() as s:
        snapshots = list(
            s.scalars(
                select(StateSnapshotRow).where(StateSnapshotRow.user_id == _U).order_by(StateSnapshotRow.id)
            ).all()
        )
    assert len(snapshots) == 1
    assert snapshots[0].reason == "before_import"
    assert snapshots[0].note == "unit test"


def test_replace_state_for_reset_keeps_prizes(store: SqlStore) -> None:
    def grant_then_reset(s, _now_ms):
        balance = locked_balance(s, _U)
        balance.credits = 50000
        balance.tokens = 30
        return ActionMutation(result=ConvertResult(amount=50))

    store.run_server_action(
        username=_U, client_action_id="act-grant", action_type="test.grant", mutator=grant_then_reset
    )
    pre_reset = store.state_dump(_U)
    assert pre_reset.balance == BalanceRead(credits_millis=50000, tokens=30)

    def reset(s, _now_ms):
        store.replace_state_for_reset(s, _U)
        return ActionMutation(result=ResetResult(reset=True))

    store.run_server_action(
        username=_U,
        client_action_id="act-reset",
        action_type="data.reset",
        mutator=reset,
        snapshot_reason="before_reset",
    )

    state = store.state_dump(_U)
    assert state.balance == BalanceRead(credits_millis=0, tokens=0)
    assert state.sessions == []
    assert state.prize_log == []
    assert len(state.prizes) == 6  # default catalog preserved


def test_db_check_constraint_rejects_negative_credits(store: SqlStore) -> None:
    """The CHECK constraint is the last line of defence if a buggy mutator misses a pre-flight check."""

    def goes_negative(s, _now_ms):
        balance = locked_balance(s, _U)
        balance.credits = -1
        return ActionMutation(result=ConvertResult(amount=0))

    with pytest.raises(IntegrityError):
        store.run_server_action(username=_U, client_action_id="act-bug", action_type="test.bug", mutator=goes_negative)


def test_state_persists_across_reopen(db_url: str) -> None:
    store_a = SqlStore(db_url)

    def grant(s, _now_ms):
        balance = locked_balance(s, _U)
        balance.credits = 13000
        return ActionMutation(result=ConvertResult(amount=13))

    store_a.run_server_action(username=_U, client_action_id="reopen-1", action_type="test.grant", mutator=grant)

    store_b = SqlStore(db_url)
    assert store_b.state_dump(_U).balance.credits_millis == 13000


def test_two_users_share_db_without_collision(store: SqlStore) -> None:
    """Two users on the same store have independent balances, sessions, ledgers."""

    def grant_alice(s, _now_ms):
        locked_balance(s, "alice").credits += 10000
        return ActionMutation(result=ConvertResult(amount=10))

    def grant_bob(s, _now_ms):
        locked_balance(s, "bob").credits += 99000
        return ActionMutation(result=ConvertResult(amount=99))

    # Same client_action_id used for both — must NOT collide since the
    # unique constraint is (user_id, client_action_id).
    store.run_server_action(username="alice", client_action_id="dup-id", action_type="t", mutator=grant_alice)
    store.run_server_action(username="bob", client_action_id="dup-id", action_type="t", mutator=grant_bob)

    assert store.state_dump("alice").balance.credits_millis == 10000
    assert store.state_dump("bob").balance.credits_millis == 99000
    # Each user's ledger lists only their own row.
    assert len(store.list_ledger_events("alice")) == 1
    assert len(store.list_ledger_events("bob")) == 1


def test_casino_stats_aggregates_server_resolved_only(store: SqlStore) -> None:
    """`casino_stats` buckets server_resolved game_events by wager type and
    by UTC day, ignoring legacy `client_reported` rows entirely."""
    # Three roulette spins on red (2 wins, 1 loss), one slots triple, one
    # blackjack win, plus one stray `client_reported` row that must NOT count.
    _seed_game_events(
        store,
        [
            _game_event(
                "ce-1",
                "roulette",
                _roulette_outcome("red", True),
                wager=10,
                payout=20,
                occurred_at_ms=1_778_200_000_000,  # 2026-05-08
            ),
            _game_event(
                "ce-2",
                "roulette",
                _roulette_outcome("red", True),
                wager=10,
                payout=20,
                occurred_at_ms=1_778_200_001_000,  # 2026-05-08
            ),
            _game_event(
                "ce-3",
                "roulette",
                _roulette_outcome("red", False),
                wager=10,
                payout=0,
                occurred_at_ms=1_778_300_000_000,  # 2026-05-09
            ),
            _game_event(
                "ce-4",
                "slots",
                _slots_outcome("triple"),
                wager=5,
                payout=100,
                occurred_at_ms=1_778_200_002_000,  # 2026-05-08
            ),
            _game_event(
                "ce-5",
                "blackjack",
                _blackjack_outcome("win"),
                wager=4,
                payout=8,
                occurred_at_ms=1_778_200_003_000,  # 2026-05-08
            ),
            # Legacy `client_reported` row — pre-2026-05-07. Excluded from casino_stats.
            _game_event(
                "legacy",
                "roulette",
                _roulette_outcome("black", False),
                wager=99,
                payout=0,
                source="client_reported",
                occurred_at_ms=1_746_000_000_000,
            ),
        ],
    )

    result = store.casino_stats(_U)

    assert result.username == _U
    assert result.since_date == "2026-05-07"
    assert result.event_count == 5  # legacy row excluded

    games_by_name = {g.game: g for g in result.games}
    roulette = games_by_name["roulette"]
    red = next(b for b in roulette.buckets if b.key == "red")
    assert red.count == 3
    assert red.wins == 2
    assert red.wagered == 30
    assert red.returned == 40
    assert red.net == 10
    assert red.payout_rate == pytest.approx(2 / 3)
    assert red.rtp == pytest.approx(40 / 30)
    # Theoretical for red on a 37-pocket wheel: P(win) = 18/37, RTP = 36/37.
    assert red.theoretical_payout_rate == pytest.approx(18 / 37)
    assert red.theoretical_rtp == pytest.approx(36 / 37)
    assert red.expected_returned == pytest.approx(30 * 36 / 37)
    assert red.expected_net == pytest.approx((30 * 36 / 37) - 30)
    assert red.fair_win_lower_tail_probability == pytest.approx(1 - (18 / 37) ** 3)

    # Roulette total covers all 3 roulette spins (no black/etc.).
    assert roulette.total.label == "All actual wagers"
    assert roulette.total.count == 3
    assert roulette.total.theoretical_payout_rate == pytest.approx(18 / 37)
    assert roulette.total.theoretical_rtp == pytest.approx(36 / 37)
    assert roulette.total.expected_returned == pytest.approx(30 * 36 / 37)
    assert roulette.total.expected_net == pytest.approx((30 * 36 / 37) - 30)
    assert roulette.total.fair_win_lower_tail_probability == pytest.approx(1 - (18 / 37) ** 3)
    # Timeline buckets across two UTC days.
    assert [b.date for b in roulette.timeline] == ["2026-05-08", "2026-05-09"]
    assert roulette.timeline[0].count == 2
    assert roulette.timeline[1].count == 1

    slots = games_by_name["slots"]
    triple = next(b for b in slots.buckets if b.key == "triple")
    assert triple.count == 1
    assert triple.wins == 1
    assert triple.rtp == pytest.approx(20.0)
    assert triple.theoretical_rtp is not None

    blackjack = games_by_name["blackjack"]
    win = next(b for b in blackjack.buckets if b.key == "win")
    assert win.count == 1
    # No theoretical RTP for blackjack.
    assert win.theoretical_rtp is None


def test_casino_stats_roulette_total_theory_uses_actual_wager_mix(store: SqlStore) -> None:
    """The roulette total row has no fake uniform strategy assumption.

    It reports the expected hit rate and returned tokens for the actual
    historical wager sequence: one even-money red wager and one single-number
    wager here.
    """
    _seed_game_events(
        store,
        [
            _game_event("roulette-mix-1", "roulette", _roulette_outcome("red", False), wager=10, payout=0),
            _game_event(
                "roulette-mix-2",
                "roulette",
                _roulette_outcome("number", False, bet_number=7, multiplier=36),
                wager=5,
                payout=0,
            ),
        ],
    )

    roulette = next(g for g in store.casino_stats(_U).games if g.game == "roulette")

    assert roulette.total.count == 2
    assert roulette.total.wagered == 15
    assert roulette.total.theoretical_payout_rate == pytest.approx(((18 / 37) + (1 / 37)) / 2)
    assert roulette.total.theoretical_rtp == pytest.approx(36 / 37)
    assert roulette.total.theoretical_ev_per_credit == pytest.approx(-1 / 37)
    assert roulette.total.expected_returned == pytest.approx(15 * 36 / 37)
    assert roulette.total.expected_net == pytest.approx(-15 / 37)
    assert roulette.total.fair_win_lower_tail_probability == pytest.approx((19 / 37) * (36 / 37))
    red = next(b for b in roulette.buckets if b.key == "red")
    number = next(b for b in roulette.buckets if b.key == "number")
    assert red.fair_win_lower_tail_probability == pytest.approx(19 / 37)
    assert number.fair_win_lower_tail_probability == pytest.approx(36 / 37)


def test_casino_stats_empty_for_fresh_user(store: SqlStore) -> None:
    result = store.casino_stats(_U)
    assert result.event_count == 0
    for game in result.games:
        assert game.total.count == 0
        assert game.timeline == []
        for bucket in game.buckets:
            assert bucket.count == 0
            assert bucket.rtp is None


def _seed_blackjack_events(store: SqlStore, fixtures: list[tuple[str, int, int, dict[str, object]]]) -> None:
    _seed_game_events(
        store,
        [
            _game_event(client_event_id, "blackjack", outcome, wager=wager, payout=payout)
            for client_event_id, wager, payout, outcome in fixtures
        ],
    )


def test_casino_stats_blackjack_summary_and_outcome_freq(store: SqlStore) -> None:
    """Summary counts split W/L/P/blackjack/bust and exclude pushes from win-rate."""
    fixtures: list[tuple[str, int, int, dict[str, object]]] = [
        ("h1", 2, 5, _blackjack_outcome("blackjack", dealer_cards=[{"rank": "6", "suit": "♥"}])),
        ("h2", 1, 2, _blackjack_outcome("win", dealer_cards=[{"rank": "7", "suit": "♥"}])),
        ("h3", 1, 2, _blackjack_outcome("dealerBust", dealer_cards=[{"rank": "5", "suit": "♦"}])),
        ("h4", 1, 1, _blackjack_outcome("push", dealer_cards=[{"rank": "K", "suit": "♣"}])),
        ("h5", 1, 0, _blackjack_outcome("lose", dealer_cards=[{"rank": "A", "suit": "♠"}])),
        ("h6", 1, 0, _blackjack_outcome("bust", dealer_cards=[{"rank": "10", "suit": "♥"}])),
    ]
    _seed_blackjack_events(store, fixtures)

    bj = next(g for g in store.casino_stats(_U).games if g.game == "blackjack").blackjack
    assert bj is not None
    assert bj.summary.count == 6
    assert bj.summary.wins == 3  # blackjack + win + dealerBust
    assert bj.summary.losses == 2  # lose + bust
    assert bj.summary.pushes == 1
    assert bj.summary.blackjacks == 1
    assert bj.summary.busts == 1
    # 3 wins / (3 wins + 2 losses) — push excluded.
    assert bj.summary.win_rate_excl_push == pytest.approx(3 / 5)
    assert bj.summary.blackjack_rate == pytest.approx(1 / 6)

    freq_by_key = {f.key: f for f in bj.outcome_freq}
    assert {f.key for f in bj.outcome_freq} == {"blackjack", "win", "dealerBust", "push", "lose", "bust"}
    assert freq_by_key["blackjack"].count == 1
    assert freq_by_key["blackjack"].freq == pytest.approx(1 / 6)
    assert freq_by_key["blackjack"].avg_wager == pytest.approx(2.0)
    assert freq_by_key["win"].avg_wager == pytest.approx(1.0)


def test_casino_stats_blackjack_by_dealer_upcard_collapses_face_cards(store: SqlStore) -> None:
    """J/Q/K and 10 share one bucket; A is separate. Slices retain real W/L/P stats."""
    fixtures: list[tuple[str, int, int, dict[str, object]]] = [
        # Two hands with dealer-K → bucketed under "10"
        ("k1", 2, 4, _blackjack_outcome("win", dealer_cards=[{"rank": "K", "suit": "♠"}])),
        ("k2", 2, 0, _blackjack_outcome("lose", dealer_cards=[{"rank": "K", "suit": "♣"}])),
        # One hand with dealer-Q → also under "10"
        ("q1", 2, 0, _blackjack_outcome("bust", dealer_cards=[{"rank": "Q", "suit": "♦"}])),
        # One ace upcard (player-favouring loss)
        ("a1", 1, 0, _blackjack_outcome("lose", dealer_cards=[{"rank": "A", "suit": "♥"}])),
    ]
    _seed_blackjack_events(store, fixtures)

    bj = next(g for g in store.casino_stats(_U).games if g.game == "blackjack").blackjack
    assert bj is not None
    by_upcard = {s.key: s for s in bj.by_dealer_upcard}

    ten = by_upcard["10"]
    assert ten.count == 3
    assert ten.wins == 1
    assert ten.losses == 2
    assert ten.pushes == 0
    assert ten.wagered == 6
    assert ten.returned == 4
    assert ten.net == -2
    assert ten.rtp == pytest.approx(4 / 6)
    assert ten.ev_per_credit == pytest.approx(-2 / 6)

    ace = by_upcard["A"]
    assert ace.count == 1
    assert ace.losses == 1

    # Other upcards present but empty.
    for k in ("2", "3", "4", "5", "6", "7", "8", "9"):
        assert by_upcard[k].count == 0
        assert by_upcard[k].rtp is None


def test_casino_stats_blackjack_by_doubled_separates_doubled_from_baseline(store: SqlStore) -> None:
    """Doubled and not-doubled hands aggregate independently."""
    fixtures: list[tuple[str, int, int, dict[str, object]]] = [
        # Doubled win (initial wager 2 → 4 after double → pays 8)
        ("d1", 4, 8, _blackjack_outcome("win", doubled=True, dealer_cards=[{"rank": "6", "suit": "♥"}])),
        # Doubled loss
        ("d2", 4, 0, _blackjack_outcome("lose", doubled=True, dealer_cards=[{"rank": "10", "suit": "♥"}])),
        # Baseline (no-double) win
        ("n1", 1, 2, _blackjack_outcome("win", dealer_cards=[{"rank": "5", "suit": "♥"}])),
        # Baseline loss
        ("n2", 1, 0, _blackjack_outcome("bust", dealer_cards=[{"rank": "7", "suit": "♥"}])),
    ]
    _seed_blackjack_events(store, fixtures)

    bj = next(g for g in store.casino_stats(_U).games if g.game == "blackjack").blackjack
    assert bj is not None
    by_doubled = {s.key: s for s in bj.by_doubled}

    assert by_doubled["doubled"].count == 2
    assert by_doubled["doubled"].wagered == 8
    assert by_doubled["doubled"].returned == 8
    assert by_doubled["doubled"].net == 0
    assert by_doubled["doubled"].ev_per_credit == pytest.approx(0.0)

    assert by_doubled["not_doubled"].count == 2
    assert by_doubled["not_doubled"].wagered == 2
    assert by_doubled["not_doubled"].returned == 2
    assert by_doubled["not_doubled"].ev_per_credit == pytest.approx(0.0)


def test_casino_stats_blackjack_field_unset_for_other_games(store: SqlStore) -> None:
    """`blackjack` is populated only on the blackjack game entry, not roulette/slots."""
    result = store.casino_stats(_U)
    for game in result.games:
        if game.game == "blackjack":
            assert game.blackjack is not None
        else:
            assert game.blackjack is None


if __name__ == "__main__":
    pytest_bazel.main()
