import json

import pytest_bazel

from finance.evidence.manifold import load_market


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


if __name__ == "__main__":
    pytest_bazel.main()
