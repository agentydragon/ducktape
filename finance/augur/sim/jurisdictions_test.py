"""Tests for the jurisdiction YAML loader."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.jurisdictions import IncomeTax, Jurisdiction, StatutoryAmount, TaxBracket, load_jurisdiction


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
