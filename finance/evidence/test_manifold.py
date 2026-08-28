import json
from datetime import UTC, datetime

import polars as pl
import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.evidence.manifold import ManifoldBet, bets_frame, load_market, prob_at


# Field shapes mirror real /v0 payloads (verified live 2026-06-10).
def _bet(bet_id: str, created_time: int, prob_after: float | None, **overrides: object) -> dict[str, object]:
    bet: dict[str, object] = {
        "id": bet_id,
        "betId": bet_id,
        "contractId": "a3k1RgxAVYiAU1qeA5Su",
        "createdTime": created_time,
        "probBefore": 0.5,
        "probAfter": prob_after,
        "amount": 10.0,
        "outcome": "YES",
        "isRedemption": False,
        "userId": "user1",
        "fees": {"creatorFee": 0},
    }
    bet.update(overrides)
    return bet


def _jsonl(rows: list[dict[str, object]]) -> bytes:
    return "\n".join(json.dumps(row) for row in rows).encode() + b"\n"


def _at(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def test_load_market_multiple_choice_has_no_probability() -> None:
    multiple_choice = {
        "id": "ikSUiiNS8MwAI75RwEJf",
        "url": "https://manifold.markets/x/y",
        "outcomeType": "MULTIPLE_CHOICE",
        "answers": [{"id": "answer1", "text": "Biden"}],
    }
    market = load_market(json.dumps(multiple_choice).encode())
    assert market.probability is None
    assert market.outcome_type == "MULTIPLE_CHOICE"


def test_bets_frame_empty() -> None:
    frame = bets_frame(b"")
    assert frame.is_empty()
    assert frame["created_time"].dtype == pl.Datetime("ms", "UTC")


def test_bet_record_strictly_validates() -> None:
    # A bet without an id is corrupt mirror data, not something to skip silently.
    bad = _bet("b1", 1_000, 0.3)
    del bad["id"], bad["betId"]
    with pytest.raises(ValidationError):
        ManifoldBet.model_validate(bad)


def test_prob_at_is_at_or_before() -> None:
    bets = bets_frame(_jsonl([_bet("b1", 1_000, 0.3), _bet("b2", 2_000, 0.5), _bet("b3", 3_000, 0.7)]))
    assert prob_at(bets, _at(2_000)) == 0.5  # boundary bet counts
    assert prob_at(bets, _at(1_500)) == 0.3
    assert prob_at(bets, _at(9_999)) == 0.7
    assert prob_at(bets, _at(500)) is None


def test_prob_at_skips_null_prob_after_and_empty_frame() -> None:
    bets = bets_frame(_jsonl([_bet("b1", 1_000, 0.3), _bet("b2", 2_000, None)]))
    assert prob_at(bets, _at(5_000)) == 0.3
    assert prob_at(bets_frame(b""), _at(5_000)) is None


def test_prob_at_multiple_choice_per_answer() -> None:
    rows = [
        _bet("b1", 1_000, 0.2, answerId="answer1"),
        _bet("b2", 2_000, 0.8, answerId="answer2"),
        _bet("b3", 3_000, 0.4, answerId="answer1"),
    ]
    bets = bets_frame(_jsonl(rows))
    answer1 = bets.filter(pl.col("answer_id") == "answer1")
    assert prob_at(answer1, _at(9_999)) == 0.4
    assert prob_at(answer1, _at(1_500)) == 0.2


if __name__ == "__main__":
    pytest_bazel.main()
