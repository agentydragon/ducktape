"""Deterministic RNG replay tests."""

from __future__ import annotations

import pytest_bazel

from x.study_casino.games import BLACKJACK_DECKS, RNG_VERSION, make_shoe, spin_roulette, spin_slots
from x.study_casino.rng import ActionRngFactory, AuditedRandom

_SECRET = b"test-auditable-rng-secret-with-enough-bytes"


def _factory() -> ActionRngFactory:
    return ActionRngFactory(secret=_SECRET, rng_version=RNG_VERSION, rng_key_id="test-key")


def _rng(action_id: str, action_type: str, request_body: dict) -> AuditedRandom:
    return _factory().for_action(
        user_id="u", client_action_id=action_id, action_type=action_type, request_body=request_body
    )


def test_replaying_seed_material_reproduces_roulette_result_and_calls():
    request = {"client_action_id": "spin-1", "wager_credits": 1, "bet_type": "red", "bet_number": None}
    rng = _rng("spin-1", "casino.roulette.spin", request)
    settlement = spin_roulette(1, "red", None, rng)
    audit = rng.audit()

    replay = AuditedRandom.from_seed_material_json(
        secret=_SECRET,
        rng_version=audit.rng_version,
        rng_key_id=audit.rng_key_id,
        seed_material_json=audit.seed_material_json,
    )
    replayed = spin_roulette(1, "red", None, replay)

    assert replayed == settlement
    assert replay.audit().calls == audit.calls
    assert audit.calls[0].purpose == "roulette.wheel_index"
    assert audit.calls[0].result["value"] == settlement.outcome.result_index


def test_slots_weighted_draws_are_logged_by_reel():
    request = {"client_action_id": "slots-1", "wager_credits": 2}
    rng = _rng("slots-1", "casino.slots.spin", request)
    settlement = spin_slots(2, rng)

    calls = rng.audit().calls
    assert [call.purpose for call in calls] == ["slots.reel.0", "slots.reel.1", "slots.reel.2"]
    assert [call.method for call in calls] == ["weighted_choice", "weighted_choice", "weighted_choice"]
    assert [call.result["item_id"] for call in calls] == settlement.outcome.symbols


def test_blackjack_shuffle_replays_exact_shoe_order():
    request = {"client_action_id": "bj-1", "wager_credits": 3}
    rng = _rng("bj-1", "blackjack.deal", request)
    shoe = make_shoe(rng)
    audit = rng.audit()

    replay = AuditedRandom.from_seed_material_json(
        secret=_SECRET,
        rng_version=audit.rng_version,
        rng_key_id=audit.rng_key_id,
        seed_material_json=audit.seed_material_json,
    )
    replayed_shoe = make_shoe(replay)

    assert replayed_shoe == shoe
    assert len(audit.calls) == BLACKJACK_DECKS * 52 - 1
    assert audit.calls[0].method == "shuffle_swap"
    assert audit.calls[0].parameters["upper"] == BLACKJACK_DECKS * 52
    assert replay.audit().calls == audit.calls


def test_seed_digest_changes_when_action_material_changes():
    request = {"client_action_id": "spin-1", "wager_credits": 1, "bet_type": "red", "bet_number": None}
    a = _rng("spin-1", "casino.roulette.spin", request).audit()
    b = _rng("spin-2", "casino.roulette.spin", request | {"client_action_id": "spin-2"}).audit()

    assert a.seed_digest_hex != b.seed_digest_hex


if __name__ == "__main__":
    pytest_bazel.main()
