from __future__ import annotations

from enum import StrEnum

from pydantic import Field, NonNegativeFloat, model_validator

from finance.augur.api.schemas import ApiModel, Percentage


class TaxRegime(StrEnum):
    CALIFORNIA_PROP13 = "california_prop13"
    CALIFORNIA_OWNER_OCCUPIED = "california_owner_occupied"
    CALIFORNIA_INVESTMENT_PROPERTY = "california_investment_property"
    SAN_FRANCISCO_SECURED_PROPERTY_TAX = "san_francisco_secured_property_tax"
    SAN_FRANCISCO_TRANSFER_TAX = "san_francisco_transfer_tax"
    VALLEJO_PROPERTY_TAX = "vallejo_property_tax"
    MARE_ISLAND_SPECIAL_ASSESSMENTS = "mare_island_special_assessments"
    CALIFORNIA_TRANSFER_TAX = "california_transfer_tax"
    FEDERAL_MORTGAGE_INTEREST = "federal_mortgage_interest"
    RENTAL_DEPRECIATION = "rental_depreciation"
    DEPRECIATION_RECAPTURE = "depreciation_recapture"
    FEDERAL_CAPITAL_GAINS = "federal_capital_gains"
    CALIFORNIA_INCOME_TAX = "california_income_tax"
    PRIMARY_RESIDENCE_EXCLUSION = "primary_residence_exclusion"


class LocalRegulation(ApiModel):
    property_tax_regime: TaxRegime = Field(
        description="Primary property-tax regime to put on a selected property for this location."
    )
    default_tax_regimes: tuple[TaxRegime, ...] = Field(
        min_length=1, description="Tax regimes enabled by default for scenarios in this location."
    )
    property_tax_annual_pct: Percentage = Field(
        description="Annual ad-valorem property-tax rate applied to the Prop 13 assessed value."
    )
    local_transfer_tax_pct: Percentage = Field(
        default=0, description="Local transfer-tax rate applied when the property is sold."
    )
    special_assessment_annual_usd: NonNegativeFloat = Field(
        default=0, description="Fixed annual local special assessment added to property-tax cash flow."
    )
    notes: str = Field(
        default="",
        description=(
            "Human-readable source and modeling notes for this location. Empty string is "
            "fine when the deployment yaml omits the field; it's metadata, not load-bearing."
        ),
    )

    @model_validator(mode="after")
    def _validate_tax_regime_defaults(self) -> LocalRegulation:
        duplicates = sorted(
            {regime for regime in self.default_tax_regimes if self.default_tax_regimes.count(regime) > 1}
        )
        if duplicates:
            raise ValueError(f"default_tax_regimes must not contain duplicates: {duplicates}")
        if self.property_tax_regime not in self.default_tax_regimes:
            raise ValueError("default_tax_regimes must include property_tax_regime")
        return self


def tax_regimes_for_local_regulation(
    local_regulation: LocalRegulation,
    *,
    existing_tax_regimes: tuple[TaxRegime, ...] = (),
    owner_occupied: bool,
    rented: bool,
) -> tuple[TaxRegime, ...]:
    """Combine a location's modeled tax-regime defaults with caller-derived
    owner-occupancy and rental signals. Pure regulation logic — no scenario
    types — so this module stays a leaf in the API dependency graph."""
    regimes = [
        *existing_tax_regimes,
        *local_regulation.default_tax_regimes,
        TaxRegime.CALIFORNIA_OWNER_OCCUPIED if owner_occupied else TaxRegime.CALIFORNIA_INVESTMENT_PROPERTY,
    ]
    if owner_occupied:
        regimes.append(TaxRegime.PRIMARY_RESIDENCE_EXCLUSION)
    if rented:
        regimes.append(TaxRegime.RENTAL_DEPRECIATION)
        regimes.append(TaxRegime.DEPRECIATION_RECAPTURE)
    return tuple(dict.fromkeys(regimes))
