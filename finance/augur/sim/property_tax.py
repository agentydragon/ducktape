"""The authority that assesses one parcel under Proposition 13 and bills its secured tax."""

from dataclasses import dataclass
from fractions import Fraction

from finance.augur.sim.actor import Actor, MonthOpened
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import Demand, PropertyTax
from finance.augur.sim.ids import AccountId, AgentId, PropertyId
from finance.augur.sim.market_path import MarketStatement
from finance.augur.sim.money import round_ratio, scaled
from finance.augur.sim.property import PropertyStatement, ScheduledPurchase

# R&TC 51(a)(1)(C): the inflation factor is the CPI change "rounded to the nearest one-thousandth
# of 1 percent" (BOE Letter To Assessors No. 2026/002).
_FACTOR_QUANTUM = 100_000


@dataclass(frozen=True, kw_only=True)
class PropertyTaxPolicy:
    """Who pays the parcel's tax to whom, over which months; month 0 is January of `start_year`."""

    property_id: PropertyId
    owner_agent_id: AgentId
    from_account_id: AccountId
    tax_authority_agent_id: AgentId
    tax_authority_account_id: AccountId
    start_year: int
    start_month: int
    end_month: int | None


def calendar_year(policy: PropertyTaxPolicy, month: int) -> int:
    return policy.start_year + month // 12


def fiscal_year(policy: PropertyTaxPolicy, month: int) -> int:
    """The year the July-June fiscal year holding `month` starts in."""
    return calendar_year(policy, month) - (month % 12 < 6)


@dataclass(frozen=True)
class _Roll:
    """A fiscal year's enrolled value, and whether the owner held the homeowners' exemption on its lien date."""

    value: int
    exempt: bool


class PropertyTaxBill(Demand):
    amount: int
    effect: PropertyTax


class PropertyTaxAuthority(Actor[MonthOpened | PropertyStatement | MarketStatement, PropertyTaxBill]):
    """Keeps one parcel's assessed value and bills its secured tax in monthly twelfths.

    Assessed value is set to the price on purchase, grown on each January lien date by that year's
    factor, and raised by new construction at its cost when completed. A fiscal year's bill is
    (base rate + the rate area's debt rate) × (the value enrolled on its lien date, less the
    homeowners' exemption if the owner then lived there), rounded to the quantum; month `j` of the
    fiscal year pays its cumulative twelfths, so the months of a fiscal year sum to its bill. A
    fiscal year whose lien date preceded the purchase is billed on the price from the month after it,
    without the exemption. Not modeled: supplemental bills on new construction, Proposition 8
    reductions and the two installment dates.
    """

    def __init__(self, policy: PropertyTaxPolicy, purchase: ScheduledPurchase) -> None:
        self.policy = policy
        self.purchase = purchase
        self.law = purchase.parcel.situs
        self.property: PropertyStatement | None = None
        self.market: MarketStatement | None = None
        # The parcel's assessed value while held, and the value each fiscal year's bill is on.
        self.assessed_value: int | None = None
        self.roll: dict[int, _Roll] = {}
        # The modeled CPI at the latest January, which a simulated lien year's factor is measured from.
        self.january_cpi: int | None = None

    def factor(self, lien_year: int, cpi: int | None) -> Fraction:
        """The lien year's inflation factor: the Board of Equalization's where published.

        Deviation: a simulated lien year's factor is the change in the world's modeled national CPI
        from the previous January, rounded as R&TC 51 rounds and capped; the law measures
        California's CPI from October to October.
        """
        law = self.law
        if lien_year <= max(law.inflation_factors, default=lien_year - 1):
            if lien_year not in law.inflation_factors:
                raise ValueError(f"no published inflation factor for lien year {lien_year}")
            return law.inflation_factors[lien_year]
        if cpi is None or self.january_cpi is None:
            raise ValueError(f"the simulated lien year {lien_year} needs the modeled CPI of it and the year before")
        change = Fraction(cpi, self.january_cpi) - 1
        rounded = Fraction(round_ratio(change.numerator * _FACTOR_QUANTUM, change.denominator), _FACTOR_QUANTUM)
        return min(1 + rounded, 1 + law.inflation_cap)

    def handle(self, message: MonthOpened | PropertyStatement | MarketStatement) -> list[PropertyTaxBill]:
        if isinstance(message, PropertyStatement):
            self.property = message
            return []
        if isinstance(message, MarketStatement):
            self.market = message
            return []
        policy, month = self.policy, message.month
        property_ = self.property if self.property is not None and self.property.month == month else None
        cpi = (
            None if self.market is None or self.market.month != month or self.market.cpi is None else self.market.cpi[0]
        )
        lien = month % 12 == 0
        if property_ is not None and property_.active:
            self.assess(property_, month, cpi if lien else None)
        if lien:
            self.january_cpi = cpi
        if (
            property_ is None
            or not property_.active
            or property_.purchase_month >= month
            or policy.start_month > month
            or (policy.end_month is not None and month > policy.end_month)
        ):
            return []
        fiscal = fiscal_year(policy, month)
        roll = self.roll[fiscal]
        taxable = max(0, roll.value - self.law.homeowners_exemption) if roll.exempt else roll.value
        bill = self.law.secured_bill(taxable, fiscal)
        elapsed = (month % 12 - 6) % 12
        return [
            PropertyTaxBill(
                cause_id=f"{policy.property_id}_property_tax_m{month}",
                obligation_type="property_tax",
                from_account=AccountRef(agent_id=policy.owner_agent_id, account_id=policy.from_account_id),
                to_account=AccountRef(
                    agent_id=policy.tax_authority_agent_id, account_id=policy.tax_authority_account_id
                ),
                amount=round_ratio(bill * (elapsed + 1), 12) - round_ratio(bill * elapsed, 12),
                effect=PropertyTax(policy.owner_agent_id, property_.rented_fraction_ppb),
            )
        ]

    def assess(self, property_: PropertyStatement, month: int, cpi: int | None) -> None:
        """This month's change to the assessed value, and the roll a lien date or the purchase sets."""
        policy, price = self.policy, self.purchase.purchase_price
        year = calendar_year(policy, month)
        if self.assessed_value is None:
            self.assessed_value = price
            self.roll[fiscal_year(policy, month)] = _Roll(price, exempt=False)
            if month % 12 < 6:
                self.roll[year] = _Roll(price, exempt=False)
        if month % 12 == 0:
            if property_.purchase_month < month:
                self.assessed_value = scaled(self.assessed_value, self.factor(year, cpi), "factored assessed value")
            self.roll[year] = _Roll(self.assessed_value, exempt=property_.owner_occupied)
        self.assessed_value += property_.new_construction
