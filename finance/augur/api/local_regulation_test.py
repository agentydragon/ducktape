from __future__ import annotations

import pytest_bazel

from finance.augur.api.local_regulation import LocalRegulation, TaxRegime, tax_regimes_for_local_regulation


def test_owner_occupied_with_rooms_rented_keeps_owner_occupied_treatment(
    san_francisco_regulation: LocalRegulation,
) -> None:
    regimes = tax_regimes_for_local_regulation(san_francisco_regulation, owner_occupied=True, rented=True)

    assert san_francisco_regulation.property_tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION in regimes
    assert TaxRegime.RENTAL_DEPRECIATION in regimes
    assert TaxRegime.CALIFORNIA_OWNER_OCCUPIED in regimes


def test_investment_property_treatment_when_owner_does_not_occupy(san_francisco_regulation: LocalRegulation) -> None:
    regimes = tax_regimes_for_local_regulation(san_francisco_regulation, owner_occupied=False, rented=True)

    assert TaxRegime.CALIFORNIA_INVESTMENT_PROPERTY in regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION not in regimes
    assert TaxRegime.RENTAL_DEPRECIATION in regimes


def test_existing_tax_regimes_are_preserved_and_deduplicated(san_francisco_regulation: LocalRegulation) -> None:
    regimes = tax_regimes_for_local_regulation(
        san_francisco_regulation,
        owner_occupied=True,
        rented=False,
        existing_tax_regimes=(TaxRegime.CALIFORNIA_PROP13, TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS),
    )

    assert TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS in regimes
    assert regimes.count(TaxRegime.CALIFORNIA_PROP13) == 1


if __name__ == "__main__":
    pytest_bazel.main()
