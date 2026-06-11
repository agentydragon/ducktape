import json

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.evidence.kalshi import load_market

# Shape mirrors a real /trade-api/v2/markets/{ticker} response (verified live
# 2026-06-10): prices come as stringified decimals, sizes as fixed-point strings.
_RESPONSE = {
    "market": {
        "ticker": "KXIPOOPENAI-26DEC31",
        "title": "OpenAI IPO before 2027?",
        "yes_sub_title": "Before Dec 31, 2026",
        "rules_primary": "Resolves YES if OpenAI completes an IPO before the close date.",
        "yes_bid": None,
        "yes_bid_dollars": "0.1200",
        "yes_ask_dollars": "0.1500",
        "yes_bid_size_fp": "250.00",
        "yes_ask_size_fp": "800.00",
        "last_price_dollars": "0.1300",
        "volume_fp": "10250.00",
        "status": "active",
        "result": "",
        "can_close_early": True,
    }
}


def test_load_market_parses_stringified_decimals() -> None:
    market = load_market(json.dumps(_RESPONSE).encode())
    assert market.ticker == "KXIPOOPENAI-26DEC31"
    assert market.yes_bid_dollars == 0.12
    assert market.yes_ask_dollars == 0.15
    assert market.yes_bid_size_fp == 250.0
    assert market.yes_ask_size_fp == 800.0
    assert market.last_price_dollars == 0.13
    assert market.volume_fp == 10250.0
    assert market.title == "OpenAI IPO before 2027?"
    assert market.yes_sub_title == "Before Dec 31, 2026"


def test_load_market_requires_wrapper() -> None:
    with pytest.raises(ValidationError):
        load_market(json.dumps(_RESPONSE["market"]).encode())


if __name__ == "__main__":
    pytest_bazel.main()
