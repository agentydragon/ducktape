"""Tests for the jurisdiction YAML loader."""

from __future__ import annotations

import math

import pytest_bazel

from finance.augur.sim.jurisdictions import load_jurisdiction


def test_load_federal_us_has_seven_ordinary_brackets() -> None:
    fed = load_jurisdiction("federal_us")
    assert fed.jurisdiction_id == "federal_us"
    single = fed.ordinary_income_brackets["single"]
    assert len(single) == 7
    assert single[0].rate == 0.10
    assert single[0].upper_usd == 11600.0
    assert single[-1].rate == 0.37
    assert math.isinf(single[-1].upper_usd)


def test_load_federal_us_has_three_ltcg_brackets() -> None:
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    ltcg = fed.ltcg_brackets["single"]
    assert [b.rate for b in ltcg] == [0.0, 0.15, 0.20]
    assert math.isinf(ltcg[-1].upper_usd)


def test_load_california_omits_ltcg_brackets() -> None:
    """California taxes LTCG as ordinary income; the YAML doesn't
    declare separate LTCG brackets and the loader represents that
    as `ltcg_brackets = None`."""
    ca = load_jurisdiction("california")
    assert ca.jurisdiction_id == "california"
    assert ca.ltcg_brackets is None
    assert len(ca.ordinary_income_brackets["single"]) == 9


def test_standard_deduction_present_for_single() -> None:
    fed = load_jurisdiction("federal_us")
    ca = load_jurisdiction("california")
    assert fed.standard_deduction["single"] == 14600.0
    assert ca.standard_deduction["single"] == 5363.0


if __name__ == "__main__":
    pytest_bazel.main()
