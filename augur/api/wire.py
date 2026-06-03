"""Catalog/settings/calibration wire types for the public Augur API.

Pydantic models at the HTTP boundary (snake_case on the wire; the frontend camelizes),
exported to the frontend Zod schema via `augur.api.export_schema`. The three GET payloads
(`CatalogResponse`, `SettingsResponse`, `CalibrationInfo`) are built once at startup from the
deployment `Config` by `augur.api.catalog`."""

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
    """Persistence-shaped property row; join to `CatalogResponse.locations` by `location_id`."""

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

    The frontend's calibration page uses it to label the run and to seed the model/preset
    picker; the catalog itself is fixed (no picker), so this carries no catalog id."""

    label: str = Field(description="Human label for the catalog (falls back to the scored issuers when unset).")
    issuers: list[str] = Field(
        default_factory=list, description="Private-equity issuer ids the catalog scores (e.g. `openai`)."
    )


class CatalogResponse(ApiModel):
    """The deployment's property/location catalog, served at `GET /api/catalog`.

    `properties` join to `locations` by `location_id`; the product form always needs both, so
    they travel together as one resource."""

    locations: list[Location]
    properties: list[Property]

    # Lookups the server/sim build off the catalog. Plain `@property` (not a serialized field), so
    # they stay off the wire while giving every caller one spelling of the derivation.
    @property
    def properties_by_id(self) -> dict[str, Property]:
        return {property_.id: property_ for property_ in self.properties}

    @property
    def location_ids(self) -> frozenset[str]:
        return frozenset(location.id for location in self.locations)


class SettingsResponse(ApiModel):
    """Cross-cutting simulation knobs the app shell reads at mount time, served at
    `GET /api/settings`: the sampling/horizon limits, the product-panel starting values, and the
    model registry the shared controls drive."""

    max_rollout_samples: PositiveInt
    max_horizon_months: PositiveInt
    product_input_defaults: ProductInputDefaults = Field(default_factory=ProductInputDefaults)
    models: tuple[str, ...]
    default_model_id: str
