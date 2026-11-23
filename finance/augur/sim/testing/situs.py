"""Hypothetical parcels for tests whose subject is not property-tax law."""

from decimal import Decimal

from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, PropertyId
from finance.augur.sim.jurisdictions import Jurisdiction, Proposition13
from finance.augur.sim.money import USD
from finance.augur.sim.property import Parcel
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.situs import compile_situs

# Month 0 of a test world's property-tax calendar.
START_YEAR = 2024
# Who owes a property's tax unless a test names another owner.
DEFAULT_OWNER = AgentId("alice")


def flat_parcel(rate: Decimal) -> Parcel:
    """A parcel taxed `rate` on its price every year: every lien year's factor 1, no debt rate, no exemption."""
    return Parcel(
        situs=compile_situs(
            Jurisdiction(
                jurisdiction_id=JurisdictionId("test-flat-rate-area"),
                proposition_13=Proposition13(
                    base_rate=rate,
                    inflation_cap=Decimal(0),
                    inflation_factors=dict.fromkeys(range(START_YEAR, START_YEAR + 100), Decimal(1)),
                    homeowners_exemption=Decimal(0),
                    supplemental_proration={},
                ),
                debt_rates={START_YEAR - 1: Decimal(0)},
            ),
            currency=USD,
        ),
        prior_assessed_value=None,
    )


UNTAXED = flat_parcel(Decimal(0))


def property_tax(property_id: PropertyId, collector: AgentId, *, owner: AgentId = DEFAULT_OWNER) -> PropertyTaxPolicy:
    """`owner` pays the property's tax to `collector`, `checking` to `checking`, from the start of the calendar."""
    return PropertyTaxPolicy(
        property_id=property_id,
        owner_agent_id=owner,
        from_account_id=AccountId("checking"),
        tax_authority_agent_id=collector,
        tax_authority_account_id=AccountId("checking"),
        start_year=START_YEAR,
        start_month=0,
        end_month=None,
    )
