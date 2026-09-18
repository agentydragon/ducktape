"""The authority that assesses a held property's monthly tax."""

from finance.augur.sim.actor import Actor, MonthOpened
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import Demand, PropertyTax
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.money import checked_count, checked_wide, mul_div_wide
from finance.augur.sim.prepared import PreparedLocation, _PropertyPurchase, _PropertyTax
from finance.augur.sim.property import PropertyStatement


class PropertyTaxBill(Demand):
    amount: int
    effect: PropertyTax


class PropertyTaxAuthority(Actor[MonthOpened | PropertyStatement, PropertyTaxBill]):
    """Assesses one property while it is held, at the policy's rate or the location's."""

    def __init__(self, policy: _PropertyTax, purchase: _PropertyPurchase, location: PreparedLocation) -> None:
        self.policy = policy
        self.purchase = purchase
        self.location = location
        self.property: PropertyStatement | None = None

    def handle(self, message: MonthOpened | PropertyStatement) -> list[PropertyTaxBill]:
        if isinstance(message, PropertyStatement):
            self.property = message
            return []
        policy, month, property_ = self.policy, message.month, self.property
        if (
            property_ is None
            or property_.month != month
            or not property_.active
            or property_.purchase_month >= month
            or policy.start_month > month
            or (policy.end_month is not None and month > policy.end_month)
        ):
            return []
        rate = (
            self.location.annual_property_tax_rate_ppb
            if policy.annual_tax_rate_ppb is None
            else policy.annual_tax_rate_ppb
        )
        numerator = checked_wide(
            self.purchase.purchase_price * rate + self.location.annual_special_assessment * MONEY_FACTOR_SCALE,
            "property tax",
        )
        return [
            PropertyTaxBill(
                cause_id=f"{policy.property_id}_property_tax_m{month}",
                obligation_type="property_tax",
                from_account=AccountRef(agent_id=policy.owner_agent_id, account_id=policy.from_account_id),
                to_account=AccountRef(
                    agent_id=policy.tax_authority_agent_id, account_id=policy.tax_authority_account_id
                ),
                amount=checked_count(
                    mul_div_wide(numerator, 1, 12 * MONEY_FACTOR_SCALE, "property tax"), "property tax"
                ),
                effect=PropertyTax(policy.owner_agent_id, property_.rented_fraction_ppb),
            )
        ]
