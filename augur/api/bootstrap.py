from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, PositiveInt, field_validator

from augur.api.local_regulation import LocalRegulation
from augur.api.schemas import ApiModel

PropertyId = str


class ActorRole(StrEnum):
    PRIMARY_OWNER = "primary_owner"
    EQUITY_BUILDING_OCCUPANT = "equity_building_occupant"
    TENANT = "tenant"
    LANDLORD = "landlord"


class Location(ApiModel):
    id: str = Field(description="Stable relational location identity used by config, storage, and scenario joins.")
    label: str
    city: str
    state: str
    local_regulation: LocalRegulation
    notes: tuple[str, ...] = ()


class Property(ApiModel):
    """Persistence-shaped property row; join to `BootstrapResponse.locations` by `location_id`."""

    id: str = Field(description="Stable relational property identity used by selection, saved scenarios, and storage.")
    source_catalog_id: str
    source_property_id: str
    location_id: str = Field(description="Foreign key for the property's canonical location row.")
    address: str
    neighborhood: str
    type: str
    price_usd: float
    rent_estimate_usd: float | None = None
    beds: float
    baths: float
    sqft: float
    year_built: int
    hoa_monthly_usd: float = 0
    annual_tax_on_list_usd: float | None = None
    source_url: str | None = None
    image_url: str | None = None
    notes: str = Field(
        default="",
        description=(
            "Free-text human note shown in the property panel; empty string when nothing "
            "to say. Frontend renders with `whitespace-pre-line` so authors can use newlines."
        ),
    )

    @field_validator("notes", mode="before")
    @classmethod
    def _collapse_list_notes(cls, value: object) -> object:
        # CLEANUP(2026-05-25): Drop once gaffer-private/k8s/augur/properties.yaml
        #   has been migrated to single-string `notes:` (deploy currently authors
        #   per-paragraph YAML lists). Until then, fold the list into one blob.
        if isinstance(value, (list, tuple)):
            return "\n\n".join(value)
        return value


class ProductInputDefaults(ApiModel):
    """Server-driven overrides for the product input panel's starting values.

    Each field is optional: `None` means "use the frontend's hard-coded base default".
    Deployments (e.g. `gaffer-private`) drop a `product_input_defaults` block into their
    augur YAML to bias the UI toward sensible starting values for their real portfolio
    without touching frontend code. `extra="forbid"` on `ApiModel` catches typos.
    """

    horizon_months: PositiveInt | None = None
    rollout_count: PositiveInt | None = None
    first_seed: int | None = None
    monthly_spend_usd: float | None = None
    spend_index: Literal["inflation", "none"] | None = None
    sell_order: str | None = None
    cash_buffer_trigger_below_usd: float | None = None
    cash_buffer_sale_usd: float | None = None
    cash_buffer_index_to_inflation: bool | None = None
    pe_lnw_floor_usd: float | None = None
    pe_index_floor_to_inflation: bool | None = None
    monthly_rent_usd: float | None = None
    rental_location_id: str | None = None
    property_id: str | None = None
    lives_here: bool | None = None
    financing_kind: Literal["cash", "mortgage"] | None = None
    down_payment_pct: float | None = None
    mortgage_term_months: Literal[180, 360] | None = None
    annual_rate_pct: float | None = None
    annual_insurance_pct: float | None = None
    annual_maintenance_pct: float | None = None
    rental_full_property_monthly_usd: float | None = None
    rental_fraction_rented_pct: float | None = None
    rental_vacancy_pct: float | None = None
    use_rental_management: bool | None = None
    management_fee_pct: float | None = None
    leasing_fee_months: float | None = None
    avg_tenancy_months: PositiveInt | None = None


class CalibrationInfo(ApiModel):
    """The deployment's single prediction-market calibration catalog (for the calibration tab).

    Present only when the deployment configures a `calibration_catalog`. The frontend's
    calibration page uses it to label the run and to seed the model/preset picker; the catalog
    itself is fixed (no picker), so this carries no catalog id."""

    label: str = Field(description="Human label for the catalog (falls back to the issuer when unset in config).")
    issuer: str = Field(description="Private-equity issuer id the catalog scores (e.g. `openai`).")


class BootstrapResponse(ApiModel):
    locations: list[Location]
    properties: list[Property]
    default_rollout_samples: PositiveInt
    max_rollout_samples: PositiveInt
    max_horizon_months: PositiveInt
    product_input_defaults: ProductInputDefaults = Field(default_factory=ProductInputDefaults)
    exogenous_presets: tuple[str, ...]
    default_exogenous_preset_id: str
    calibration: CalibrationInfo | None = Field(
        default=None,
        description="The deployment's calibration catalog info, or None when no `calibration_catalog` is configured.",
    )
