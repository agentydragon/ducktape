"""Round-trip invariant for the untagged `ActionResult` union.

The union discriminates structurally (`extra="forbid"` + a distinct
required-field set per variant) and Pydantic tries variants left-to-right,
so a variant whose serialized form also validates as an earlier variant
would be silently mis-parsed — both on the wire and on the idempotent-replay
path, which re-reads `ledger_events.result_json` through this union
(`store._ACTION_RESULT_ADAPTER`). Each variant must round-trip to exactly
its own type.
"""

from __future__ import annotations

from typing import get_args

import pytest
import pytest_bazel
from pydantic import BaseModel, TypeAdapter

from x.study_casino.actions import (
    ActionResult,
    BlackjackHandStateResult,
    BlackjackSettlement,
    ChangelogAckResult,
    ConvertResult,
    ImportResult,
    PrizeCreateResult,
    PrizeDeleteResult,
    PrizeRedeemResult,
    ResetResult,
    RouletteActionResult,
    SessionAddPastResult,
    SessionCompleteResult,
    SessionCreditsDeltaResult,
    SlotsActionResult,
)
from x.study_casino.events import Card

_ADAPTER: TypeAdapter[ActionResult] = TypeAdapter(ActionResult)

_PLAYER = [Card(rank="A", suit="♠"), Card(rank="K", suit="♥")]
_DEALER = [Card(rank="9", suit="♦"), Card(rank="10", suit="♣")]

_EXAMPLES: list[BaseModel] = [
    SessionCompleteResult(
        session_id="s1",
        seconds=360,
        credits_earned_millis=6_000,
        daily_bonus_millis=0,
        streak_days=1,
        streak_bonus_percent=1,
    ),
    SessionAddPastResult(session_id="s2", credits_earned_millis=1_000),
    SessionCreditsDeltaResult(session_id="s3", credits_delta_millis=-500),
    ConvertResult(amount=5),
    PrizeCreateResult(prize_id="p1", name="Ice cream", cost=3, user="auragon"),
    PrizeDeleteResult(prize_id="p1", user="auragon"),
    PrizeRedeemResult(redemption_id="r1", prize_id="p1", cost=3),
    ChangelogAckResult(acked_through=2),
    ImportResult(imported=True),
    ResetResult(reset=True),
    SlotsActionResult(
        symbols=["seven", "seven", "seven"],
        glyphs=["7", "7", "7"],
        label="Triple seven",
        payout_kind="triple",
        payout_tokens=10,
    ),
    RouletteActionResult(
        bet_type="red",
        bet_number=None,
        multiplier=2,
        result_color="red",
        result_number=18,
        result_index=29,
        won=True,
        payout_tokens=20,
    ),
    BlackjackHandStateResult(
        hand_id="h1",
        phase="done",
        current_wager=10,
        player_cards=_PLAYER,
        dealer_cards=_DEALER,
        player_value=21,
        dealer_value=19,
        settlement=BlackjackSettlement(
            outcome="blackjack",
            text="Blackjack!",
            player_cards=_PLAYER,
            dealer_cards=_DEALER,
            player_value=21,
            dealer_value=19,
            player_blackjack=True,
            dealer_blackjack=False,
            payout_tokens=25,
        ),
    ),
]


def test_examples_cover_every_variant() -> None:
    assert {type(e) for e in _EXAMPLES} == set(get_args(ActionResult))


@pytest.mark.parametrize("example", _EXAMPLES, ids=lambda e: type(e).__name__)
def test_round_trips_to_own_variant(example: BaseModel) -> None:
    assert type(_ADAPTER.validate_json(example.model_dump_json())) is type(example)


if __name__ == "__main__":
    pytest_bazel.main()
