"""Scheduled and recurring obligations as the counterparties that demand them."""

from finance.augur.sim.actor import Actor, MonthOpened
from finance.augur.sim.claims import Demand, OrdinaryDeduction
from finance.augur.sim.prepared import PreparedAmount, PreparedObligation
from finance.augur.sim.property import PropertyStatement
from finance.augur.sim.schedule import is_due


class Bill(Demand):
    """Priced by the ledger when registered, so an indexed amount follows its series."""

    amount: PreparedAmount
    deduction: OrdinaryDeduction | None


class Biller(Actor[MonthOpened | PropertyStatement, Bill]):
    """One obligation: bills its payer in the months it is due.

    A bill attached to a property is posted that property's statement first and bills
    only while the property is held, deducting by the share that is let.
    """

    def __init__(self, spec: PreparedObligation) -> None:
        self.spec = spec
        self.property: PropertyStatement | None = None

    def handle(self, message: MonthOpened | PropertyStatement) -> list[Bill]:
        if isinstance(message, PropertyStatement):
            self.property = message
            return []
        spec, month = self.spec, message.month
        if not is_due(spec.schedule, month):
            return []
        if spec.property_id is None:
            fraction = spec.deductible_fraction_ppb
        elif self.property is None or self.property.month != month or not self.property.active:
            return []
        else:
            fraction = self.property.rented_fraction_ppb
        return [
            Bill(
                cause_id=f"{spec.obligation_id}_m{month}",
                obligation_type=spec.obligation_type,
                from_account=spec.from_account,
                to_account=spec.to_account,
                amount=spec.amount_due,
                deduction=OrdinaryDeduction(fraction) if spec.deduction_category == "ordinary" else None,
            )
        ]
