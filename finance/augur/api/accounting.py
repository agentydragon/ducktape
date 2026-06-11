from __future__ import annotations

from enum import StrEnum

from pydantic import Field, NonNegativeFloat, NonNegativeInt

from finance.augur.api.schemas import ApiModel


class LotAssetClass(StrEnum):
    PUBLIC_SECURITY = "public_security"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"
    PROPERTY = "property"


class LiabilityType(StrEnum):
    MORTGAGE = "mortgage"
    TAX_PAYABLE = "tax_payable"
    PARTNER_CLAIM = "partner_claim"
    CREDIT_FACILITY = "credit_facility"


class TaxLot(ApiModel):
    lot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-:.]*$")
    asset_class: LotAssetClass
    owner_actor_id: str
    source_account_id: str | None = None
    source_asset_id: str | None = None
    property_id: str | None = None
    quantity: NonNegativeFloat | None = None
    cost_basis_usd: NonNegativeFloat
    acquisition_month_index: NonNegativeInt = 0


class LotDisposition(ApiModel):
    lot_disposition_id: str
    journal_entry_id: str
    rollout_index: NonNegativeInt
    month_index: NonNegativeInt
    lot_id: str
    asset_class: LotAssetClass
    proceeds_usd: NonNegativeFloat
    cost_basis_usd: NonNegativeFloat
    realized_gain_usd: float
    taxable_gain_usd: float
    quantity_sold: NonNegativeFloat | None = None
    tax_expense_usd: NonNegativeFloat = 0.0
    path_set_id: str | None = None
    exogenous_path_id: str | None = None
    scenario_input_id: str | None = None
    projection_trajectory_id: str | None = None


class LiabilityState(ApiModel):
    liability_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-:.]*$")
    liability_type: LiabilityType
    actor_id: str
    creditor_id: str | None = None
    counterparty_actor_id: str | None = None
    property_id: str | None = None
    balance_usd: float
