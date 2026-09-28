"""Scheduled and recurring obligations as the counterparties that demand them."""

from dataclasses import dataclass, field

from finance.augur.sim.actor import Actor, MonthOpened
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import Demand, OrdinaryDeduction
from finance.augur.sim.ids import PropertyId
from finance.augur.sim.income import TransferDeductionCategory
from finance.augur.sim.market_path import Amount
from finance.augur.sim.property import PropertyStatement
from finance.augur.sim.schedule import Schedule, is_due


class Bill(Demand):
    """Priced by the ledger when registered, so an indexed amount follows its series."""

    amount: Amount
    deduction: OrdinaryDeduction | None


@dataclass(kw_only=True)
class Biller(Actor[MonthOpened | PropertyStatement, Bill]):
    """One obligation: bills its payer in the months it is due.

    A bill attached to a property is posted that property's statement first and bills
    only while the property is held, deducting by the share that is let.
    """

    obligation_id: str
    obligation_type: str
    from_account: AccountRef
    to_account: AccountRef
    amount_due: Amount
    property_id: PropertyId | None
    deduction_category: TransferDeductionCategory | None
    deductible_fraction_ppb: int
    schedule: Schedule
    property: PropertyStatement | None = field(init=False, default=None)

    def handle(self, message: MonthOpened | PropertyStatement) -> list[Bill]:
        if isinstance(message, PropertyStatement):
            self.property = message
            return []
        month = message.month
        if not is_due(self.schedule, month):
            return []
        if self.property_id is None:
            fraction = self.deductible_fraction_ppb
        elif self.property is None or self.property.month != month or not self.property.active:
            return []
        else:
            fraction = self.property.rented_fraction_ppb
        return [
            Bill(
                cause_id=f"{self.obligation_id}_m{month}",
                obligation_type=self.obligation_type,
                from_account=self.from_account,
                to_account=self.to_account,
                amount=self.amount_due,
                deduction=OrdinaryDeduction(fraction) if self.deduction_category == "ordinary" else None,
            )
        ]
