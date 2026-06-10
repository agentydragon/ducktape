import json

import pytest
import pytest_bazel

from finance.evidence.polymarket import load_market

# Shape mirrors a real gamma response (verified live 2026-06-10).
_GAMMA_MARKET = {
    "id": "253591",
    "question": "Will Donald Trump win the 2024 US Presidential Election?",
    "conditionId": "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917",
    "slug": "will-donald-trump-win-the-2024-us-presidential-election",
    "description": "This market will resolve to Yes if Donald Trump wins.",
    "bestBid": 0.997,
    "bestAsk": 0.998,
    "lastTradePrice": 1,
    "volumeNum": 1531479284.504353,
    "closed": True,
    "umaResolutionStatus": "resolved",
}


def test_load_market_from_condition_id_list() -> None:
    # `?condition_ids=` responses are one-element lists, stored verbatim.
    market = load_market(json.dumps([_GAMMA_MARKET]).encode())
    assert market.id == "253591"
    assert market.condition_id == "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917"
    assert market.best_bid == 0.997
    assert market.best_ask == 0.998
    assert market.last_trade_price == 1.0
    assert market.volume_num == pytest.approx(1531479284.5, rel=1e-6)
    assert market.closed


def test_load_market_from_bare_object() -> None:
    market = load_market(json.dumps(_GAMMA_MARKET).encode())
    assert market.slug == "will-donald-trump-win-the-2024-us-presidential-election"


def test_load_market_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="too few items"):
        load_market(b"[]")


if __name__ == "__main__":
    pytest_bazel.main()
