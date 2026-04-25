"""Validators reject every malformed economy state we can think of."""

from __future__ import annotations

import pytest
import pytest_bazel
from pycrdt import Map

from x.auragon_study_casino.doc_shape import Casino
from x.auragon_study_casino.validators import ValidationError, validate


def test_clean_empty_casino_passes() -> None:
    validate(Casino.empty())


def test_negative_credits_rejected() -> None:
    casino = Casino.empty()
    casino.balance["credits"] = -1
    with pytest.raises(ValidationError) as exc_info:
        validate(casino)
    assert exc_info.value.rule == "credits_nonneg"


def test_negative_tokens_rejected() -> None:
    casino = Casino.empty()
    casino.balance["tokens"] = -5
    with pytest.raises(ValidationError) as exc_info:
        validate(casino)
    assert exc_info.value.rule == "tokens_nonneg"


def test_zero_cost_prize_rejected() -> None:
    casino = Casino.empty()
    casino.prizes["custom"] = Map()
    casino.prizes["custom"]["name"] = "Bogus"
    casino.prizes["custom"]["cost"] = 0
    with pytest.raises(ValidationError) as exc_info:
        validate(casino)
    assert exc_info.value.rule == "prize_cost"


def test_blank_session_subject_rejected() -> None:
    casino = Casino.empty()
    casino.sessions["s1"] = Map()
    casino.sessions["s1"]["subject"] = ""
    casino.sessions["s1"]["seconds"] = 60
    casino.sessions["s1"]["ended_at_ms"] = 0
    with pytest.raises(ValidationError) as exc_info:
        validate(casino)
    assert exc_info.value.rule == "session_subject"


def test_negative_session_seconds_rejected() -> None:
    casino = Casino.empty()
    casino.sessions["s1"] = Map()
    casino.sessions["s1"]["subject"] = "Biochem"
    casino.sessions["s1"]["seconds"] = -10
    casino.sessions["s1"]["ended_at_ms"] = 1
    with pytest.raises(ValidationError) as exc_info:
        validate(casino)
    assert exc_info.value.rule == "session_seconds"


if __name__ == "__main__":
    pytest_bazel.main()
