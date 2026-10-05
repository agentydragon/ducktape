"""Tests for the jurisdiction YAML loader."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.jurisdictions import IncomeTax, Jurisdiction, StatutoryAmount, TaxBracket, load_jurisdiction


def test_load_federal_us_has_seven_ordinary_brackets() -> None:
    fed = load_jurisdiction(JurisdictionId("federal_us")).income_tax
    assert fed is not None
    single = fed.ordinary_income_brackets["single"]
    assert len(single) == 7
    assert single[0].rate == Decimal("0.10")
    assert single[0].upper == 11600.0
    assert single[-1].rate == Decimal("0.37")
    assert single[-1].upper == "Infinity"


def test_load_federal_us_has_three_ltcg_brackets() -> None:
    fed = load_jurisdiction(JurisdictionId("federal_us")).income_tax
    assert fed is not None
    assert fed.ltcg_brackets is not None
    ltcg = fed.ltcg_brackets["single"]
    assert [b.rate for b in ltcg] == [0, Decimal("0.15"), Decimal("0.20")]
    assert ltcg[-1].upper == "Infinity"


def test_standard_deduction_present_for_single() -> None:
    fed = load_jurisdiction(JurisdictionId("federal_us")).income_tax
    ca = load_jurisdiction(JurisdictionId("california")).income_tax
    assert fed is not None
    assert ca is not None
    assert fed.standard_deduction["single"] == 14600.0
    assert ca.standard_deduction["single"] == 5363.0


def test_every_dollar_amount_is_tagged_indexed_or_fixed() -> None:
    """An amount nobody tagged would silently stay nominal under CPI indexing."""
    federal = load_jurisdiction(JurisdictionId("federal_us")).income_tax
    assert federal is not None
    data = federal.model_dump()
    del data["indexation"][StatutoryAmount.NET_INVESTMENT_INCOME_TAX]
    with pytest.raises(ValidationError):
        IncomeTax.model_validate(data)
    data["net_investment_income_tax"] = None
    IncomeTax.model_validate(data)


def test_unknown_law_is_refused_rather_than_ignored() -> None:
    """A key nothing reads would promise law the engine does not apply."""
    data = load_jurisdiction(JurisdictionId("california")).model_dump()
    data["proposition_13"]["decline_in_value"] = "proposition_8"
    with pytest.raises(ValidationError) as refusal:
        Jurisdiction.model_validate(data)
    assert [(error["type"], error["loc"]) for error in refusal.value.errors()] == [
        ("extra_forbidden", ("proposition_13", "decline_in_value"))
    ]


def test_float_rate_is_refused_as_inexact() -> None:
    with pytest.raises(TypeError):
        TaxBracket.model_validate({"upper": "Infinity", "rate": 0.1})


if __name__ == "__main__":
    pytest_bazel.main()
