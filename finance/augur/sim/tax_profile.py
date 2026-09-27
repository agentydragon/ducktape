"""A taxpayer's filing status, jurisdictions and tax-payment routing."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from finance.augur.sim.ids import CHECKING, AccountId, AgentId, JurisdictionId
from finance.augur.sim.money import NonNegativeCurrencyAmount


class FilingStatus(StrEnum):
    """Federal/state filing status. Today only single-filer is wired through the tax + §121
    math; adding a new variant requires touching every place that branches on filing status
    (bracket lookup keys in jurisdiction YAMLs, §121 cap table in `_apply_property_sale`,
    standard-deduction lookup, …). The enum makes this an explicit blocker on every
    callsite rather than a string typo silently falling through to a missing-key error."""

    SINGLE = "single"


class TaxProfile(BaseModel):
    """A taxed agent's tax-time configuration. At spike 1 only single filers are modeled;
    later layers add MFJ / HoH and any filing-status-driven branching."""

    agent_id: AgentId
    filing_status: FilingStatus = FilingStatus.SINGLE
    jurisdiction_ids: list[JurisdictionId] = Field(
        description='Ordered list of taxing authorities — typically `["federal_us", "california"]` for a CA resident.'
    )
    tax_authority_agent_id: AgentId = Field(
        description="Destination of tax-payment transfers — a bookkeeping sink, not a taxed agent itself."
    )
    payment_account_id: AccountId = Field(
        default=CHECKING, description="The agent's account the engine debits for estimated-tax and true-up payments."
    )
    tax_authority_account_id: AccountId = Field(
        default=CHECKING, description="The matching credit account on the tax authority's side."
    )
    prior_year_tax: NonNegativeCurrencyAmount = Field(
        default=Decimal(0),
        description=(
            "Aggregate safe-harbor target used to size quarterly estimated payments. If "
            "0, no quarterly estimates are emitted and the January true-up pays the full "
            "accrued tax."
        ),
    )
