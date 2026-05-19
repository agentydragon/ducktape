from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from augur.core.scenario_set import TaxFilingStatus, TaxProfile

Bracket = tuple[float | None, float]

_ANNUAL_TAX_PARAMETERS_PATH = Path(__file__).with_name("annual_tax_parameters.yaml")
_TAX_FILING_STATUSES = tuple(TaxFilingStatus)


class _TaxParameterModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TaxBracketParameters(_TaxParameterModel):
    upper_bound_usd: float | None
    rate: float

    @model_validator(mode="after")
    def _validate_bracket(self) -> _TaxBracketParameters:
        if self.upper_bound_usd is not None and self.upper_bound_usd <= 0.0:
            raise ValueError("upper_bound_usd must be positive or null")
        _validate_rate("rate", self.rate)
        return self


class _FederalLongTermCapitalGainThresholds(_TaxParameterModel):
    zero_rate_ceiling_usd: float
    fifteen_rate_ceiling_usd: float

    @model_validator(mode="after")
    def _validate_thresholds(self) -> _FederalLongTermCapitalGainThresholds:
        if self.zero_rate_ceiling_usd < 0.0:
            raise ValueError("zero_rate_ceiling_usd must be non-negative")
        if self.fifteen_rate_ceiling_usd <= self.zero_rate_ceiling_usd:
            raise ValueError("fifteen_rate_ceiling_usd must be greater than zero_rate_ceiling_usd")
        return self


class _BehavioralHealthServicesTaxParameters(_TaxParameterModel):
    threshold_usd: float
    rate: float

    @model_validator(mode="after")
    def _validate_behavioral_health_tax(self) -> _BehavioralHealthServicesTaxParameters:
        if self.threshold_usd < 0.0:
            raise ValueError("threshold_usd must be non-negative")
        _validate_rate("rate", self.rate)
        return self


class _NetInvestmentIncomeTaxParameters(_TaxParameterModel):
    rate: float
    magi_threshold_usd_by_filing_status: dict[TaxFilingStatus, float]

    @model_validator(mode="after")
    def _validate_niit(self) -> _NetInvestmentIncomeTaxParameters:
        _validate_rate("rate", self.rate)
        _validate_status_map("magi_threshold_usd_by_filing_status", self.magi_threshold_usd_by_filing_status)
        for status in _TAX_FILING_STATUSES:
            if self.magi_threshold_usd_by_filing_status[status] < 0.0:
                raise ValueError(f"magi_threshold_usd_by_filing_status.{status.value} must be non-negative")
        return self


class _BaseJurisdictionTaxParameters(_TaxParameterModel):
    tax_year: int
    standard_deduction_usd_by_filing_status: dict[TaxFilingStatus, float]
    ordinary_brackets_by_filing_status: dict[TaxFilingStatus, tuple[_TaxBracketParameters, ...]]

    @model_validator(mode="after")
    def _validate_base_jurisdiction(self) -> _BaseJurisdictionTaxParameters:
        if self.tax_year <= 0:
            raise ValueError("tax_year must be positive")
        _validate_status_map("standard_deduction_usd_by_filing_status", self.standard_deduction_usd_by_filing_status)
        _validate_status_map("ordinary_brackets_by_filing_status", self.ordinary_brackets_by_filing_status)
        for status in _TAX_FILING_STATUSES:
            if self.standard_deduction_usd_by_filing_status[status] < 0.0:
                raise ValueError(f"standard_deduction_usd_by_filing_status.{status.value} must be non-negative")
            _validate_brackets(
                f"ordinary_brackets_by_filing_status.{status.value}", self.ordinary_brackets_by_filing_status[status]
            )
        return self


class _FederalTaxParameters(_BaseJurisdictionTaxParameters):
    long_term_capital_gain_thresholds_usd_by_filing_status: dict[TaxFilingStatus, _FederalLongTermCapitalGainThresholds]
    unrecaptured_1250_gain_max_rate: float
    salt_cap_usd: float
    qualified_residence_interest_principal_cap_usd: float
    net_investment_income_tax: _NetInvestmentIncomeTaxParameters

    @model_validator(mode="after")
    def _validate_federal(self) -> _FederalTaxParameters:
        _validate_status_map(
            "long_term_capital_gain_thresholds_usd_by_filing_status",
            self.long_term_capital_gain_thresholds_usd_by_filing_status,
        )
        _validate_rate("unrecaptured_1250_gain_max_rate", self.unrecaptured_1250_gain_max_rate)
        if self.salt_cap_usd < 0.0:
            raise ValueError("salt_cap_usd must be non-negative")
        if self.qualified_residence_interest_principal_cap_usd <= 0.0:
            raise ValueError("qualified_residence_interest_principal_cap_usd must be positive")
        return self


class _CaliforniaTaxParameters(_BaseJurisdictionTaxParameters):
    behavioral_health_services_tax: _BehavioralHealthServicesTaxParameters


class _AnnualTaxParameters(_TaxParameterModel):
    federal: _FederalTaxParameters
    california: _CaliforniaTaxParameters


def _validate_rate(field_name: str, rate: float) -> None:
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")


def _validate_status_map(field_name: str, value_by_status: dict[TaxFilingStatus, Any]) -> None:
    expected = set(_TAX_FILING_STATUSES)
    actual = set(value_by_status)
    if actual != expected:
        missing = ", ".join(status.value for status in _TAX_FILING_STATUSES if status not in actual) or "none"
        unexpected = ", ".join(sorted(str(status) for status in actual - expected)) or "none"
        expected_list = ", ".join(status.value for status in _TAX_FILING_STATUSES)
        raise ValueError(
            f"{field_name} must define exactly these filing statuses: {expected_list}; "
            f"missing: {missing}; unexpected: {unexpected}"
        )


def _validate_brackets(field_name: str, brackets: tuple[_TaxBracketParameters, ...]) -> None:
    if not brackets:
        raise ValueError(f"{field_name} must contain at least one bracket")
    lower_bound = 0.0
    for index, bracket in enumerate(brackets):
        if bracket.upper_bound_usd is None:
            if index != len(brackets) - 1:
                raise ValueError(f"{field_name} may only use null upper_bound_usd on the final bracket")
            return
        if bracket.upper_bound_usd <= lower_bound:
            raise ValueError(f"{field_name} upper_bound_usd values must be strictly increasing")
        lower_bound = bracket.upper_bound_usd
    raise ValueError(f"{field_name} must end with an unbounded bracket")


def _validate_annual_tax_parameters(payload: Any) -> _AnnualTaxParameters:
    return _AnnualTaxParameters.model_validate(payload)


def _load_annual_tax_parameters(path: Path = _ANNUAL_TAX_PARAMETERS_PATH) -> _AnnualTaxParameters:
    return _validate_annual_tax_parameters(yaml.safe_load(path.read_text(encoding="utf-8")))


def _deduction_table(parameters: _BaseJurisdictionTaxParameters) -> dict[TaxFilingStatus, float]:
    return {status: parameters.standard_deduction_usd_by_filing_status[status] for status in _TAX_FILING_STATUSES}


def _bracket_table(parameters: _BaseJurisdictionTaxParameters) -> dict[TaxFilingStatus, tuple[Bracket, ...]]:
    return {
        status: tuple(
            (bracket.upper_bound_usd, bracket.rate) for bracket in parameters.ordinary_brackets_by_filing_status[status]
        )
        for status in _TAX_FILING_STATUSES
    }


_ANNUAL_TAX_PARAMETERS = _load_annual_tax_parameters()

FEDERAL_STANDARD_DEDUCTION_2026 = _deduction_table(_ANNUAL_TAX_PARAMETERS.federal)
FEDERAL_ORDINARY_BRACKETS_2026 = _bracket_table(_ANNUAL_TAX_PARAMETERS.federal)
FEDERAL_LONG_TERM_CAPITAL_GAIN_THRESHOLDS_2026 = {
    status: (
        _ANNUAL_TAX_PARAMETERS.federal.long_term_capital_gain_thresholds_usd_by_filing_status[
            status
        ].zero_rate_ceiling_usd,
        _ANNUAL_TAX_PARAMETERS.federal.long_term_capital_gain_thresholds_usd_by_filing_status[
            status
        ].fifteen_rate_ceiling_usd,
    )
    for status in _TAX_FILING_STATUSES
}
CALIFORNIA_STANDARD_DEDUCTION_2025 = _deduction_table(_ANNUAL_TAX_PARAMETERS.california)
CALIFORNIA_ORDINARY_BRACKETS_2025 = _bracket_table(_ANNUAL_TAX_PARAMETERS.california)
CALIFORNIA_BEHAVIORAL_HEALTH_SERVICES_TAX_THRESHOLD_USD = (
    _ANNUAL_TAX_PARAMETERS.california.behavioral_health_services_tax.threshold_usd
)
CALIFORNIA_BEHAVIORAL_HEALTH_SERVICES_TAX_RATE = _ANNUAL_TAX_PARAMETERS.california.behavioral_health_services_tax.rate
FEDERAL_UNRECAPTURED_1250_GAIN_MAX_RATE = _ANNUAL_TAX_PARAMETERS.federal.unrecaptured_1250_gain_max_rate
FEDERAL_SALT_CAP_USD = _ANNUAL_TAX_PARAMETERS.federal.salt_cap_usd
FEDERAL_QUALIFIED_RESIDENCE_INTEREST_PRINCIPAL_CAP_USD = (
    _ANNUAL_TAX_PARAMETERS.federal.qualified_residence_interest_principal_cap_usd
)
FEDERAL_NIIT_RATE = _ANNUAL_TAX_PARAMETERS.federal.net_investment_income_tax.rate
FEDERAL_NIIT_MAGI_THRESHOLDS_USD = {
    status: _ANNUAL_TAX_PARAMETERS.federal.net_investment_income_tax.magi_threshold_usd_by_filing_status[status]
    for status in _TAX_FILING_STATUSES
}


@dataclass(frozen=True)
class AnnualSaleTaxAllocation:
    federal_income_tax_usd: np.ndarray
    california_income_tax_usd: np.ndarray
    total_income_tax_usd: np.ndarray
    property_sale_tax_usd: np.ndarray
    generic_sp500_sale_tax_usd: np.ndarray
    generic_crypto_sale_tax_usd: np.ndarray
    private_equity_sale_tax_usd: np.ndarray
    rental_income_tax_usd: np.ndarray


def annual_sale_tax_allocation(
    tax_profile: TaxProfile,
    *,
    month_index: np.ndarray,
    property_depreciation_recapture_usd: np.ndarray,
    taxable_property_capital_gain_usd: np.ndarray,
    generic_sp500_sale_gain_usd: np.ndarray,
    generic_crypto_sale_gain_usd: np.ndarray,
    private_equity_sale_taxable_gain_usd: np.ndarray,
    property_tax_usd: np.ndarray,
    mortgage_interest_usd: np.ndarray,
    mortgage_principal_balance_usd: np.ndarray,
    net_rental_taxable_income_usd: np.ndarray,
) -> AnnualSaleTaxAllocation:
    """Allocate annual federal and California tax created by simulated income and sale gains.

    Computes the incremental yearly tax over the scenario's baseline ordinary
    income (which is taxed independently via user payroll withholding) and
    allocates that tax back to the months and sources that generated the
    taxable income: sale gains, rental income, and the deduction effects from
    property tax (SALT, federal only, capped) and qualified-residence mortgage
    interest (federal + California, capped at interest on $750k principal).
    Federal tax includes the 3.8% net investment income tax above MAGI
    thresholds.
    """
    source_shape = property_depreciation_recapture_usd.shape
    federal_income_tax = np.zeros(source_shape, dtype="float64")
    california_income_tax = np.zeros(source_shape, dtype="float64")
    property_sale_tax = np.zeros(source_shape, dtype="float64")
    generic_sp500_sale_tax = np.zeros(source_shape, dtype="float64")
    generic_crypto_sale_tax = np.zeros(source_shape, dtype="float64")
    private_equity_sale_tax = np.zeros(source_shape, dtype="float64")
    rental_income_tax = np.zeros(source_shape, dtype="float64")

    property_recapture = np.maximum(0.0, property_depreciation_recapture_usd)
    property_capital_gain = np.maximum(0.0, taxable_property_capital_gain_usd)
    sp500_capital_gain = np.maximum(0.0, generic_sp500_sale_gain_usd)
    crypto_capital_gain = np.maximum(0.0, generic_crypto_sale_gain_usd)
    private_equity_capital_gain = np.maximum(0.0, private_equity_sale_taxable_gain_usd)
    rental_taxable_income = np.maximum(0.0, net_rental_taxable_income_usd)
    property_taxable_income = property_recapture + property_capital_gain
    sale_taxable_income = (
        property_taxable_income + sp500_capital_gain + crypto_capital_gain + private_equity_capital_gain
    )
    source_taxable_income = sale_taxable_income + rental_taxable_income

    rollout_count = source_shape[0]
    ordinary_income = np.full(rollout_count, float(tax_profile.annual_ordinary_income_usd), dtype="float64")
    baseline_federal = federal_income_tax_due_usd(
        tax_profile,
        ordinary_income_usd=ordinary_income,
        unrecaptured_1250_gain_usd=np.zeros(rollout_count, dtype="float64"),
        long_term_capital_gain_usd=np.zeros(rollout_count, dtype="float64"),
    )
    baseline_california = california_income_tax_due_usd(
        tax_profile, ordinary_income_usd=ordinary_income, capital_income_usd=np.zeros(rollout_count, dtype="float64")
    )

    for tax_year in np.unique(month_index // 12):
        year_mask = (month_index // 12) == tax_year
        year_property_recapture = np.sum(property_recapture[:, year_mask], axis=1)
        year_property_capital_gain = np.sum(property_capital_gain[:, year_mask], axis=1)
        year_sp500_capital_gain = np.sum(sp500_capital_gain[:, year_mask], axis=1)
        year_crypto_capital_gain = np.sum(crypto_capital_gain[:, year_mask], axis=1)
        year_private_equity_capital_gain = np.sum(private_equity_capital_gain[:, year_mask], axis=1)
        year_long_term_capital_gain = (
            year_property_capital_gain
            + year_sp500_capital_gain
            + year_crypto_capital_gain
            + year_private_equity_capital_gain
        )
        year_rental_income = np.sum(rental_taxable_income[:, year_mask], axis=1)
        year_source_taxable_income = np.sum(source_taxable_income[:, year_mask], axis=1)

        year_property_tax_paid = np.sum(property_tax_usd[:, year_mask], axis=1)
        year_salt_deduction = np.minimum(year_property_tax_paid, FEDERAL_SALT_CAP_USD)

        year_mortgage_interest_paid = np.sum(mortgage_interest_usd[:, year_mask], axis=1)
        year_qualified_interest_deduction = _qualified_residence_interest_deduction_usd(
            interest_paid_usd=year_mortgage_interest_paid,
            principal_balance_per_month_usd=mortgage_principal_balance_usd[:, year_mask],
        )

        year_federal_ordinary = np.maximum(
            0.0, ordinary_income + year_rental_income - year_salt_deduction - year_qualified_interest_deduction
        )
        year_california_ordinary = np.maximum(
            0.0, ordinary_income + year_rental_income - year_qualified_interest_deduction
        )

        year_federal_tax = np.maximum(
            0.0,
            federal_income_tax_due_usd(
                tax_profile,
                ordinary_income_usd=year_federal_ordinary,
                unrecaptured_1250_gain_usd=year_property_recapture,
                long_term_capital_gain_usd=year_long_term_capital_gain,
            )
            - baseline_federal,
        )
        year_california_tax = np.maximum(
            0.0,
            california_income_tax_due_usd(
                tax_profile,
                ordinary_income_usd=year_california_ordinary,
                capital_income_usd=year_property_recapture + year_long_term_capital_gain,
            )
            - baseline_california,
        )
        year_total_tax = year_federal_tax + year_california_tax

        federal_income_tax[:, year_mask] = _allocate_tax_to_months(
            year_federal_tax, source_taxable_income[:, year_mask], year_source_taxable_income
        )
        california_income_tax[:, year_mask] = _allocate_tax_to_months(
            year_california_tax, source_taxable_income[:, year_mask], year_source_taxable_income
        )
        property_sale_tax[:, year_mask] = _allocate_tax_to_months(
            year_total_tax, property_taxable_income[:, year_mask], year_source_taxable_income
        )
        generic_sp500_sale_tax[:, year_mask] = _allocate_tax_to_months(
            year_total_tax, sp500_capital_gain[:, year_mask], year_source_taxable_income
        )
        generic_crypto_sale_tax[:, year_mask] = _allocate_tax_to_months(
            year_total_tax, crypto_capital_gain[:, year_mask], year_source_taxable_income
        )
        private_equity_sale_tax[:, year_mask] = _allocate_tax_to_months(
            year_total_tax, private_equity_capital_gain[:, year_mask], year_source_taxable_income
        )
        rental_income_tax[:, year_mask] = _allocate_tax_to_months(
            year_total_tax, rental_taxable_income[:, year_mask], year_source_taxable_income
        )

    return AnnualSaleTaxAllocation(
        federal_income_tax_usd=federal_income_tax,
        california_income_tax_usd=california_income_tax,
        total_income_tax_usd=federal_income_tax + california_income_tax,
        property_sale_tax_usd=property_sale_tax,
        generic_sp500_sale_tax_usd=generic_sp500_sale_tax,
        generic_crypto_sale_tax_usd=generic_crypto_sale_tax,
        private_equity_sale_tax_usd=private_equity_sale_tax,
        rental_income_tax_usd=rental_income_tax,
    )


def _qualified_residence_interest_deduction_usd(
    *, interest_paid_usd: np.ndarray, principal_balance_per_month_usd: np.ndarray
) -> np.ndarray:
    """Cap qualified residence interest at the amount paid on the first $750k of principal.

    Rather than recomputing the post-1987 acquisition-debt rules, scale the
    actual interest paid by min(1, $750k / average annual principal balance);
    when the principal balance averages \$1.5M, half the interest is
    deductible. Average is restricted to months with a non-zero balance so an
    annual sale (final months at zero) does not artificially deflate the
    denominator.
    """
    active = principal_balance_per_month_usd > 0
    months_active = np.sum(active, axis=1)
    sum_principal = np.sum(principal_balance_per_month_usd, axis=1)
    average_principal = np.divide(
        sum_principal, months_active, out=np.zeros_like(sum_principal), where=months_active > 0
    )
    deductible_fraction = np.divide(
        FEDERAL_QUALIFIED_RESIDENCE_INTEREST_PRINCIPAL_CAP_USD,
        np.maximum(average_principal, FEDERAL_QUALIFIED_RESIDENCE_INTEREST_PRINCIPAL_CAP_USD),
        out=np.ones_like(average_principal),
        where=average_principal > 0,
    )
    return np.asarray(interest_paid_usd * deductible_fraction, dtype="float64")


def federal_income_tax_due_usd(
    tax_profile: TaxProfile,
    *,
    ordinary_income_usd: np.ndarray,
    unrecaptured_1250_gain_usd: np.ndarray,
    long_term_capital_gain_usd: np.ndarray,
) -> np.ndarray:
    filing_status = tax_profile.filing_status
    standard_deduction = _federal_standard_deduction(tax_profile)
    ordinary_income = np.maximum(0.0, ordinary_income_usd)
    recapture_gain = np.maximum(0.0, unrecaptured_1250_gain_usd)
    long_term_capital_gain = np.maximum(0.0, long_term_capital_gain_usd)

    ordinary_taxable_income = np.maximum(0.0, ordinary_income - standard_deduction)
    deduction_after_ordinary = np.maximum(0.0, standard_deduction - ordinary_income)
    recapture_taxable_income = np.maximum(0.0, recapture_gain - deduction_after_ordinary)
    deduction_after_recapture = np.maximum(0.0, deduction_after_ordinary - recapture_gain)
    long_term_capital_gain_taxable = np.maximum(0.0, long_term_capital_gain - deduction_after_recapture)

    ordinary_tax = _progressive_tax(ordinary_taxable_income, FEDERAL_ORDINARY_BRACKETS_2026[filing_status])
    recapture_as_ordinary_tax = (
        _progressive_tax(
            ordinary_taxable_income + recapture_taxable_income, FEDERAL_ORDINARY_BRACKETS_2026[filing_status]
        )
        - ordinary_tax
    )
    recapture_tax = np.minimum(
        recapture_as_ordinary_tax, recapture_taxable_income * FEDERAL_UNRECAPTURED_1250_GAIN_MAX_RATE
    )
    long_term_capital_gain_tax = _federal_long_term_capital_gain_tax(
        filing_status, ordinary_taxable_income + recapture_taxable_income, long_term_capital_gain_taxable
    )
    # NIIT (3.8%) on the smaller of (net investment income, MAGI - threshold).
    # NII includes capital gains and depreciation recapture; future phases add
    # qualified dividends and interest. MAGI is approximated as ordinary
    # income plus investment income — close enough for households without
    # foreign-earned-income exclusions or excluded muni interest.
    net_investment_income = recapture_gain + long_term_capital_gain
    magi = ordinary_income + net_investment_income
    niit_threshold = FEDERAL_NIIT_MAGI_THRESHOLDS_USD[filing_status]
    niit_base = np.minimum(net_investment_income, np.maximum(0.0, magi - niit_threshold))
    niit = niit_base * FEDERAL_NIIT_RATE
    total_tax = ordinary_tax.copy()
    total_tax += recapture_tax
    total_tax += long_term_capital_gain_tax
    total_tax += niit
    return total_tax


def california_income_tax_due_usd(
    tax_profile: TaxProfile, *, ordinary_income_usd: np.ndarray, capital_income_usd: np.ndarray
) -> np.ndarray:
    filing_status = tax_profile.filing_status
    taxable_income = np.maximum(
        0.0,
        np.maximum(0.0, ordinary_income_usd)
        + np.maximum(0.0, capital_income_usd)
        - _california_standard_deduction(tax_profile),
    )
    ordinary_tax = _progressive_tax(taxable_income, CALIFORNIA_ORDINARY_BRACKETS_2025[filing_status])
    behavioral_health_services_tax = (
        np.maximum(0.0, taxable_income - CALIFORNIA_BEHAVIORAL_HEALTH_SERVICES_TAX_THRESHOLD_USD)
        * CALIFORNIA_BEHAVIORAL_HEALTH_SERVICES_TAX_RATE
    )
    total_tax = ordinary_tax.copy()
    total_tax += behavioral_health_services_tax
    return total_tax


def _federal_standard_deduction(tax_profile: TaxProfile) -> float:
    if tax_profile.federal_standard_deduction_usd is not None:
        return float(tax_profile.federal_standard_deduction_usd)
    return FEDERAL_STANDARD_DEDUCTION_2026[tax_profile.filing_status]


def _california_standard_deduction(tax_profile: TaxProfile) -> float:
    if tax_profile.california_standard_deduction_usd is not None:
        return float(tax_profile.california_standard_deduction_usd)
    return CALIFORNIA_STANDARD_DEDUCTION_2025[tax_profile.filing_status]


def _federal_long_term_capital_gain_tax(
    filing_status: TaxFilingStatus, ordinary_taxable_income_usd: np.ndarray, gain_usd: np.ndarray
) -> np.ndarray:
    zero_rate_ceiling, fifteen_rate_ceiling = FEDERAL_LONG_TERM_CAPITAL_GAIN_THRESHOLDS_2026[filing_status]
    gain = np.maximum(0.0, gain_usd)
    zero_rate_room = np.maximum(0.0, zero_rate_ceiling - ordinary_taxable_income_usd)
    zero_rate_gain = np.minimum(gain, zero_rate_room)
    remaining_gain = np.maximum(0.0, gain - zero_rate_gain)
    fifteen_rate_room = np.maximum(
        0.0, fifteen_rate_ceiling - np.maximum(ordinary_taxable_income_usd, zero_rate_ceiling)
    )
    fifteen_rate_gain = np.minimum(remaining_gain, fifteen_rate_room)
    twenty_rate_gain = np.maximum(0.0, remaining_gain - fifteen_rate_gain)
    tax = np.zeros_like(gain, dtype="float64")
    tax += fifteen_rate_gain * 0.15
    tax += twenty_rate_gain * 0.20
    return tax


def _progressive_tax(income_usd: np.ndarray, brackets: tuple[Bracket, ...]) -> np.ndarray:
    income = np.maximum(0.0, income_usd)
    tax = np.zeros_like(income, dtype="float64")
    lower_bound = 0.0
    for upper_bound, rate in brackets:
        if upper_bound is None:
            bracket_income = np.maximum(0.0, income - lower_bound)
        else:
            bracket_income = np.minimum(np.maximum(0.0, income - lower_bound), upper_bound - lower_bound)
            lower_bound = upper_bound
        tax = tax + bracket_income * rate
    return tax


def _allocate_tax_to_months(
    tax_usd: np.ndarray, monthly_source_usd: np.ndarray, year_source_usd: np.ndarray
) -> np.ndarray:
    allocated_tax = np.zeros_like(monthly_source_usd, dtype="float64")
    np.divide(
        tax_usd[:, None] * monthly_source_usd,
        year_source_usd[:, None],
        out=allocated_tax,
        where=year_source_usd[:, None] > 0,
    )
    return allocated_tax
