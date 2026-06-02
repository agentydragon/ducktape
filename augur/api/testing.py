"""Shared test fixtures/builders for the public augur deployment fixture.

Test-only helpers reused across `augur/api` and `augur/product` test modules and the visual
test: the public testdata `Config`, and the synthetic `LocalRegulation`s the property/location
fixtures are built from."""

from __future__ import annotations

from augur.api.config import Config, load_augur_config
from augur.api.local_regulation import LocalRegulation, TaxRegime
from util.bazel.runfiles import get_required_path


def load_fixture_config() -> Config:
    """The public testdata deployment config (`augur/api/testdata/config.yaml`)."""
    return load_augur_config(get_required_path("_main/augur/api/testdata/config.yaml"))


def fixture_regulation() -> LocalRegulation:
    """Synthetic CALIFORNIA_PROP13 regulation for the public-fixture locations."""
    return LocalRegulation(
        property_tax_regime=TaxRegime.CALIFORNIA_PROP13,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
        ),
        property_tax_annual_pct=1.0,
        notes="Synthetic public fixture location.",
    )


def san_francisco_regulation() -> LocalRegulation:
    """San Francisco secured-property-tax regulation (the real SF regime stack)."""
    return LocalRegulation(
        property_tax_regime=TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
            TaxRegime.SAN_FRANCISCO_TRANSFER_TAX,
        ),
        property_tax_annual_pct=1.18,
        notes="San Francisco fixture",
    )
