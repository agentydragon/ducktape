from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from augur.core import annual_tax
from augur.core.annual_tax import annual_sale_tax_allocation, california_income_tax_due_usd, federal_income_tax_due_usd
from augur.core.scenario_set import TaxProfile


def test_tax_parameter_validation_rejects_missing_filing_status() -> None:
    payload = annual_tax._ANNUAL_TAX_PARAMETERS.model_dump(mode="json")
    del payload["federal"]["standard_deduction_usd_by_filing_status"]["head_of_household"]

    with pytest.raises(ValueError, match="standard_deduction_usd_by_filing_status must define exactly"):
        annual_tax._validate_annual_tax_parameters(payload)


def test_federal_ordinary_income_uses_2026_single_brackets_after_standard_deduction() -> None:
    tax = federal_income_tax_due_usd(
        TaxProfile(annual_ordinary_income_usd=100_000),
        ordinary_income_usd=np.asarray([100_000.0]),
        unrecaptured_1250_gain_usd=np.asarray([0.0]),
        long_term_capital_gain_usd=np.asarray([0.0]),
    )

    np.testing.assert_allclose(tax, 13_170)


def test_california_income_uses_2025_single_brackets_after_standard_deduction() -> None:
    tax = california_income_tax_due_usd(
        TaxProfile(annual_ordinary_income_usd=100_000),
        ordinary_income_usd=np.asarray([100_000.0]),
        capital_income_usd=np.asarray([0.0]),
    )

    np.testing.assert_allclose(tax, 5_207.98)


def _empty_arrays(shape: tuple[int, int]) -> dict[str, np.ndarray]:
    zeros = np.zeros(shape, dtype="float64")
    return {
        "property_depreciation_recapture_usd": zeros.copy(),
        "taxable_property_capital_gain_usd": zeros.copy(),
        "generic_sp500_sale_gain_usd": zeros.copy(),
        "generic_crypto_sale_gain_usd": zeros.copy(),
        "private_equity_sale_taxable_gain_usd": zeros.copy(),
        "property_tax_usd": zeros.copy(),
        "mortgage_interest_usd": zeros.copy(),
        "mortgage_principal_balance_usd": zeros.copy(),
        "net_rental_taxable_income_usd": zeros.copy(),
    }


def test_annual_sale_tax_allocates_stock_gain_to_sale_month() -> None:
    shape = (1, 13)
    inputs = _empty_arrays(shape)
    inputs["generic_sp500_sale_gain_usd"][:, 5] = 10_000

    allocation = annual_sale_tax_allocation(TaxProfile(), month_index=np.arange(13, dtype="int64"), **inputs)

    np.testing.assert_allclose(allocation.federal_income_tax_usd[:, 5], 0)
    np.testing.assert_allclose(allocation.california_income_tax_usd[:, 5], 42.94)
    np.testing.assert_allclose(allocation.generic_sp500_sale_tax_usd[:, 5], 42.94)
    np.testing.assert_allclose(allocation.total_income_tax_usd[:, 5], 42.94)
    np.testing.assert_allclose(allocation.total_income_tax_usd[:, :5], 0)
    np.testing.assert_allclose(allocation.total_income_tax_usd[:, 6:], 0)


def test_salt_deduction_lowers_federal_tax_on_rental_year() -> None:
    """A scenario with property tax + rental income pays less federal tax than the no-SALT case."""
    shape = (1, 12)
    inputs_with_salt = _empty_arrays(shape)
    # Rental income each month creates positive ordinary income.
    inputs_with_salt["net_rental_taxable_income_usd"][:] = 5_000.0
    # Property tax exceeding the SALT cap.
    inputs_with_salt["property_tax_usd"][:] = 1_500.0  # \$18k/yr; SALT capped at \$10k

    allocation_with_salt = annual_sale_tax_allocation(
        TaxProfile(filing_status="single"), month_index=np.arange(12, dtype="int64"), **inputs_with_salt
    )

    inputs_without_salt = _empty_arrays(shape)
    inputs_without_salt["net_rental_taxable_income_usd"][:] = 5_000.0

    allocation_without_salt = annual_sale_tax_allocation(
        TaxProfile(filing_status="single"), month_index=np.arange(12, dtype="int64"), **inputs_without_salt
    )

    federal_with = np.sum(allocation_with_salt.federal_income_tax_usd)
    federal_without = np.sum(allocation_without_salt.federal_income_tax_usd)
    assert federal_with < federal_without
    # SALT capped at \$10k; rental \$60k pushes ordinary into the 12% bracket.
    np.testing.assert_allclose(federal_without - federal_with, 10_000.0 * 0.12, atol=1.0)


def test_niit_applies_above_magi_threshold() -> None:
    """Single filer above \$200k MAGI threshold pays 3.8% NIIT on capital gains above the threshold."""
    tax = federal_income_tax_due_usd(
        TaxProfile(filing_status="single"),
        ordinary_income_usd=np.asarray([180_000.0]),
        unrecaptured_1250_gain_usd=np.asarray([0.0]),
        long_term_capital_gain_usd=np.asarray([100_000.0]),
    )
    tax_no_gain = federal_income_tax_due_usd(
        TaxProfile(filing_status="single"),
        ordinary_income_usd=np.asarray([180_000.0]),
        unrecaptured_1250_gain_usd=np.asarray([0.0]),
        long_term_capital_gain_usd=np.asarray([0.0]),
    )
    # MAGI = 280k; threshold = 200k; min(NII=100k, MAGI-threshold=80k) = 80k → 3.8% × 80k = 3,040.
    assert tax[0] - tax_no_gain[0] > 0
    # Without NIIT, the LTCG-only addition would be \$100k × 15% = \$15k. With NIIT, it should be \$15k + \$3,040.
    np.testing.assert_allclose(tax[0] - tax_no_gain[0] - 100_000.0 * 0.15, 3_040.0, atol=1.0)


def test_rental_income_increases_obligation() -> None:
    shape = (1, 12)
    inputs_with_rental = _empty_arrays(shape)
    inputs_with_rental["net_rental_taxable_income_usd"][:] = 5_000.0

    inputs_no_rental = _empty_arrays(shape)

    rental = annual_sale_tax_allocation(
        TaxProfile(filing_status="single"), month_index=np.arange(12, dtype="int64"), **inputs_with_rental
    )
    no_rental = annual_sale_tax_allocation(
        TaxProfile(filing_status="single"), month_index=np.arange(12, dtype="int64"), **inputs_no_rental
    )

    assert np.sum(rental.total_income_tax_usd) > np.sum(no_rental.total_income_tax_usd)


if __name__ == "__main__":
    pytest_bazel.main()
