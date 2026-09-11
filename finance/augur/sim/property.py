"""Property lifecycle and ledger settlement; mortgage servicing remains caller-owned."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from finance.augur.sim.accounting import Accounting, TransferOutcome
from finance.augur.sim.books import AccountRef, JournalEntry, Posting, PropertyState
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.holdings import gain_account
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, mul_div
from finance.augur.sim.mortgage import Mortgage, MortgageTerms
from finance.augur.sim.prepared import PreparedScenario, _PropertyPurchase, _PropertySale


@dataclass
class Property:
    state: PropertyState
    occupied_window: list[bool] = field(default_factory=lambda: [False] * 60)


@dataclass(frozen=True)
class Purchase:
    month: int
    cause_id: str
    property_id: str
    location_id: str
    buyer_agent_id: str
    purchase_price: int
    closing_cost: int
    adjusted_basis: int
    stake_contribution: int
    equity_ledger: int


@dataclass(frozen=True)
class Sale:
    month: int
    property_id: str
    gross_proceeds: int
    mortgage_payoff: int
    net_cash_to_owner: int
    realized_gain: int
    depreciation_recapture: int
    section_121_exclusion: int
    long_term_capital_gain: int


@dataclass(frozen=True)
class Residence:
    month: int
    agent_id: str
    property_id: str | None
    is_primary_residence: bool


@dataclass(frozen=True)
class RentedFraction:
    month: int
    property_id: str
    rented_fraction_ppb: int


@dataclass(frozen=True)
class CapitalImprovement:
    month: int
    property_id: str
    amount: int
    description: str


@dataclass(frozen=True)
class Origination:
    month: int
    cause_id: str
    liability_id: str
    agent_id: str
    payment_account_id: str
    counterparty_agent_id: str
    counterparty_account_id: str
    property_id: str
    principal: int
    annual_interest_rate_ppb: int
    term_months: int
    monthly_payment: int


def asset_account(purchase: _PropertyPurchase) -> AccountRef:
    return AccountRef(agent_id=purchase.buyer_agent_id, account_id=f"asset:property:{purchase.property_id}")


def principal(accounting: Accounting, purchase: _PropertyPurchase) -> int:
    loan = purchase.mortgage
    if loan is None:
        return 0
    return checked_count(
        -accounting.ledger.balance(
            AccountRef(agent_id=purchase.buyer_agent_id, account_id=f"liability:mortgage:{loan.liability_id}")
        ),
        "money negation",
    )


def mortgage_terms(purchase: _PropertyPurchase) -> MortgageTerms:
    financing = purchase.mortgage
    if financing is None:
        raise ValueError("purchase has no mortgage financing")
    return MortgageTerms(
        liability_id=financing.liability_id,
        property_id=purchase.property_id,
        borrower=AccountRef(agent_id=purchase.buyer_agent_id, account_id=purchase.buyer_account_id),
        lender=AccountRef(agent_id=financing.lender_agent_id, account_id=financing.lender_account_id),
        origination_month=purchase.month,
        origination_principal=financing.principal,
        annual_interest_rate_ppb=financing.annual_interest_rate_ppb,
        term_months=financing.term_months,
    )


class Properties:
    def __init__(self, scenario: PreparedScenario, accounting: Accounting) -> None:
        self.properties: dict[str, Property] = {}
        self.primary: dict[str, str | None] = {
            row.agent_id: row.property_id for row in scenario._initial_primary_residences
        }
        self.purchases: list[Purchase] = []
        self.sales: list[Sale] = []
        self.residences: list[Residence] = []
        self.rented_fractions: list[RentedFraction] = []
        self.improvements: list[CapitalImprovement] = []
        self.originations: list[Origination] = []
        for purchase in scenario._scheduled_property_purchases:
            for account in (
                asset_account(purchase),
                gain_account(purchase.buyer_agent_id),
                AccountRef(
                    agent_id=purchase.buyer_agent_id, account_id=f"expense:property-basis:{purchase.property_id}"
                ),
                AccountRef(
                    agent_id=purchase.seller_agent_id, account_id=f"equity:property-sale:{purchase.property_id}"
                ),
            ):
                accounting.ledger.ensure_account(account)
            loan = purchase.mortgage
            if loan is not None:
                for agent, prefix in (
                    (purchase.buyer_agent_id, "liability:mortgage"),
                    (purchase.buyer_agent_id, "expense:mortgage-interest"),
                    (loan.lender_agent_id, "asset:mortgage-receivable"),
                    (loan.lender_agent_id, "income:mortgage-interest"),
                    (loan.lender_agent_id, "equity:mortgage-funding"),
                ):
                    accounting.ledger.ensure_account(
                        AccountRef(agent_id=agent, account_id=f"{prefix}:{loan.liability_id}")
                    )

    def snapshots(self) -> list[PropertyState]:
        return [property_.state for property_ in self.properties.values()]

    def market_value(self, purchase: _PropertyPurchase, market: MarketPath, month: int) -> int:
        series = f"home_value:{purchase.location_id}"
        return mul_div(
            purchase.purchase_price,
            market.value(series, month),
            market.value(series, purchase.month),
            "property market value",
        )

    def assign_residences(self, scenario: PreparedScenario, month: int) -> None:
        for event in sorted(
            (event for event in scenario._primary_residence_events if event.month == month),
            key=lambda event: event.agent_id,
        ):
            self.primary[event.agent_id] = event.property_id
            self.residences.append(Residence(month, event.agent_id, event.property_id, event.property_id is not None))

    def lifecycle(
        self,
        scenario: PreparedScenario,
        accounting: Accounting,
        market: MarketPath,
        month: int,
        mortgages: Mapping[str, Mortgage],
    ) -> list[str]:
        ids = sorted(
            {event.property_id for event in scenario._property_rented_fraction_events if event.month == month}
            | {
                improvement.property_id
                for improvement in scenario._capital_improvement_events
                if improvement.month == month
            }
            | {sale.property_id for sale in scenario._property_sales if sale.month == month}
        )
        purchases = {purchase.property_id: purchase for purchase in scenario._scheduled_property_purchases}
        paid_off = []
        for id_ in ids:
            property_ = self.properties.get(id_)
            if property_ is None or not property_.state.active:
                continue
            for event in scenario._property_rented_fraction_events:
                if event.month == month and event.property_id == id_:
                    property_.state = property_.state.model_copy(
                        update={"rented_fraction_ppb": event.rented_fraction_ppb}
                    )
                    self.rented_fractions.append(RentedFraction(month, id_, event.rented_fraction_ppb))
            for improvement in scenario._capital_improvement_events:
                if improvement.month != month or improvement.property_id != id_:
                    continue
                purchase = purchases[id_]
                basis = checked_count(property_.state.building_basis + improvement.amount, "money addition")
                accounting.apply(
                    JournalEntry(
                        month=month,
                        cause_id=f"capital-improvement:{id_}:{month}",
                        postings=[
                            Posting(
                                account=AccountRef(
                                    agent_id=purchase.buyer_agent_id, account_id=purchase.buyer_account_id
                                ),
                                amount=checked_count(-improvement.amount, "money negation"),
                            ),
                            Posting(account=asset_account(purchase), amount=improvement.amount),
                        ],
                    )
                )
                property_.state = property_.state.model_copy(update={"building_basis": basis})
                self.improvements.append(CapitalImprovement(month, id_, improvement.amount, ""))
            for sale in scenario._property_sales:
                if sale.month == month and sale.property_id == id_ and property_.state.active:
                    payoff = self.sell(scenario, accounting, market, purchases[id_], sale, mortgages)
                    if payoff is not None:
                        paid_off.append(payoff)
        return paid_off

    def sell(
        self,
        scenario: PreparedScenario,
        accounting: Accounting,
        market: MarketPath,
        purchase: _PropertyPurchase,
        sale: _PropertySale,
        mortgages: Mapping[str, Mortgage],
    ) -> str | None:
        property_ = self.properties[sale.property_id]
        state = property_.state
        gross = mul_div(
            self.market_value(purchase, market, sale.month),
            MONEY_FACTOR_SCALE - sale.closing_cost_ppb,
            MONEY_FACTOR_SCALE,
            "property sale proceeds",
        )
        loan = purchase.mortgage
        payoff = 0
        paid_off = None
        if loan is not None:
            payoff = principal(accounting, purchase)
            if payoff:
                mortgage = mortgages.get(loan.liability_id)
                if mortgage is None or not mortgage.active or mortgage.terms != mortgage_terms(purchase):
                    raise ValueError("mortgage payoff needs the active servicing contract")
                paid_off = loan.liability_id
        net_cash = checked_count(gross - payoff, "money subtraction")
        capex = checked_count(state.building_basis - state.building_basis_initial, "money subtraction")
        # Sale gain excludes capitalized buyer closing costs under the current contract.
        adjusted = checked_count(
            checked_count(purchase.purchase_price + capex, "money addition") - state.cumulative_depreciation,
            "money subtraction",
        )
        gain = checked_count(gross - adjusted, "money subtraction")
        recapture = min(max(0, gain), state.cumulative_depreciation)
        remainder = max(0, checked_count(gain - recapture, "money subtraction"))
        profile = next(
            (profile for profile in scenario.tax_profiles if profile.agent_id == purchase.buyer_agent_id), None
        )
        cap = 0 if profile is None else profile.section_121_exclusion
        exclusion = min(remainder, cap) if sum(property_.occupied_window) >= 24 else 0
        long_gain = checked_count(remainder - exclusion, "money subtraction")
        property_basis = checked_count(state.adjusted_basis + capex, "money addition")
        writeoff = checked_count(
            checked_count(state.adjusted_basis - purchase.purchase_price, "money subtraction")
            + state.cumulative_depreciation,
            "money addition",
        )
        postings = [
            Posting(
                account=AccountRef(agent_id=purchase.buyer_agent_id, account_id=purchase.buyer_account_id),
                amount=net_cash,
            ),
            Posting(account=asset_account(purchase), amount=checked_count(-property_basis, "money negation")),
            Posting(
                account=AccountRef(
                    agent_id=purchase.buyer_agent_id, account_id=f"expense:property-basis:{purchase.property_id}"
                ),
                amount=writeoff,
            ),
            Posting(account=gain_account(purchase.buyer_agent_id), amount=checked_count(-gain, "money negation")),
        ]
        if loan is not None and paid_off is not None:
            postings.extend(
                [
                    Posting(
                        account=AccountRef(
                            agent_id=purchase.buyer_agent_id, account_id=f"liability:mortgage:{loan.liability_id}"
                        ),
                        amount=payoff,
                    ),
                    Posting(
                        account=AccountRef(
                            agent_id=loan.lender_agent_id, account_id=f"asset:mortgage-receivable:{loan.liability_id}"
                        ),
                        amount=checked_count(-payoff, "money negation"),
                    ),
                    Posting(
                        account=AccountRef(
                            agent_id=loan.lender_agent_id, account_id=f"equity:mortgage-funding:{loan.liability_id}"
                        ),
                        amount=payoff,
                    ),
                ]
            )
        tax = deepcopy(accounting.tax)
        tax.gain(purchase.buyer_agent_id, long_gain, long_term=True)
        if purchase.buyer_agent_id in tax.years:
            year = tax.years[purchase.buyer_agent_id]
            year.section_1250_recapture = checked_count(year.section_1250_recapture + recapture, "money addition")
        accounting.apply(
            JournalEntry(month=sale.month, cause_id=f"property-sale:{sale.property_id}", postings=postings)
        )
        accounting.tax = tax
        property_.state = state.model_copy(update={"active": False, "rented_fraction_ppb": 0, "building_basis": 0})
        if self.primary.get(purchase.buyer_agent_id) == sale.property_id:
            self.primary[purchase.buyer_agent_id] = None
        self.sales.append(
            Sale(sale.month, sale.property_id, gross, payoff, net_cash, gain, recapture, exclusion, long_gain)
        )
        return paid_off

    def purchase(
        self, scenario: PreparedScenario, accounting: Accounting, month: int, originations: Mapping[str, Mortgage]
    ) -> list[str]:
        originated = []
        for purchase in scenario._scheduled_property_purchases:
            if purchase.month != month:
                continue
            loan = purchase.mortgage
            debt = 0 if loan is None else loan.principal
            adjusted = checked_count(purchase.purchase_price + purchase.buyer_closing_cost, "money addition")
            building = checked_count(
                mul_div(
                    purchase.purchase_price,
                    MONEY_FACTOR_SCALE - purchase.land_value_fraction_ppb,
                    MONEY_FACTOR_SCALE,
                    "property building basis",
                )
                + purchase.buyer_closing_cost,
                "money addition",
            )
            stake = checked_count(purchase.down_payment + purchase.buyer_closing_cost, "money addition")
            equity = checked_count(purchase.purchase_price - debt, "money subtraction")
            buyer = AccountRef(agent_id=purchase.buyer_agent_id, account_id=purchase.buyer_account_id)
            seller = AccountRef(agent_id=purchase.seller_agent_id, account_id=purchase.seller_account_id)
            clearing = AccountRef(
                agent_id=purchase.seller_agent_id, account_id=f"equity:property-sale:{purchase.property_id}"
            )
            postings = [
                Posting(account=buyer, amount=checked_count(-stake, "money negation")),
                Posting(account=seller, amount=stake),
                Posting(account=asset_account(purchase), amount=adjusted),
                Posting(account=clearing, amount=checked_count(-stake, "money negation")),
            ]
            origination = None
            if loan is not None:
                mortgage = originations.get(loan.liability_id)
                if mortgage is None or not mortgage.active or mortgage.terms != mortgage_terms(purchase):
                    raise ValueError("mortgage origination needs the active financing contract")
                postings.extend(
                    [
                        Posting(
                            account=AccountRef(
                                agent_id=purchase.buyer_agent_id, account_id=f"liability:mortgage:{loan.liability_id}"
                            ),
                            amount=checked_count(-debt, "money negation"),
                        ),
                        Posting(
                            account=AccountRef(
                                agent_id=loan.lender_agent_id,
                                account_id=f"asset:mortgage-receivable:{loan.liability_id}",
                            ),
                            amount=debt,
                        ),
                        Posting(
                            account=AccountRef(
                                agent_id=loan.lender_agent_id, account_id=f"equity:mortgage-funding:{loan.liability_id}"
                            ),
                            amount=checked_count(-debt, "money negation"),
                        ),
                    ]
                )
                origination = Origination(
                    month,
                    f"{purchase.cause_id}_mortgage_origination",
                    loan.liability_id,
                    purchase.buyer_agent_id,
                    purchase.buyer_account_id,
                    loan.lender_agent_id,
                    loan.lender_account_id,
                    purchase.property_id,
                    debt,
                    loan.annual_interest_rate_ppb,
                    loan.term_months,
                    mortgage.monthly_payment,
                )
            else:
                postings.append(Posting(account=clearing, amount=debt))
            state = PropertyState(
                property_id=purchase.property_id,
                location_id=purchase.location_id,
                owner_agent_id=purchase.buyer_agent_id,
                purchase_month=month,
                adjusted_basis=adjusted,
                rented_fraction_ppb=purchase.rented_fraction_ppb,
                building_basis_initial=building,
                building_basis=building,
                cumulative_depreciation=0,
                depreciation_ytd=0,
                owner_occupied_months=0,
                contribution_used=stake,
                equity_ledger=equity,
                active=True,
            )
            accounting.apply(JournalEntry(month=month, cause_id=purchase.cause_id, postings=postings))
            if stake > 0 and accounting.capture != "summary":
                accounting.transfers.append(
                    TransferOutcome(month, f"{purchase.cause_id}_buyer_cash", buyer, seller, stake, None)
                )
            if origination is not None:
                self.originations.append(origination)
                originated.append(origination.liability_id)
            self.properties[purchase.property_id] = Property(state)
            self.purchases.append(
                Purchase(
                    month,
                    purchase.cause_id,
                    purchase.property_id,
                    purchase.location_id,
                    purchase.buyer_agent_id,
                    purchase.purchase_price,
                    purchase.buyer_closing_cost,
                    adjusted,
                    stake,
                    equity,
                )
            )
        return originated

    def accrue(self, accounting: Accounting, month: int) -> None:
        for property_ in self.properties.values():
            state = property_.state
            occupied = (
                state.active
                and state.rented_fraction_ppb < MONEY_FACTOR_SCALE
                and self.primary.get(state.owner_agent_id) == state.property_id
            )
            property_.occupied_window[month % 60] = occupied
            occupied_months = state.owner_occupied_months + int(occupied)
            if occupied_months >= 1 << 32:
                raise OverflowError("primary-residence occupied-month count")
            depreciation = 0
            if state.active and state.rented_fraction_ppb > 0:
                remaining = checked_count(state.building_basis - state.cumulative_depreciation, "money subtraction")
                if remaining > 0:
                    depreciation = min(
                        remaining,
                        mul_div(
                            state.building_basis,
                            state.rented_fraction_ppb,
                            MONEY_FACTOR_SCALE * 330,
                            "property monthly depreciation",
                        ),
                    )
            cumulative = checked_count(state.cumulative_depreciation + depreciation, "money addition")
            ytd = checked_count(state.depreciation_ytd + depreciation, "money addition")
            if state.owner_agent_id in accounting.tax.years:
                year = accounting.tax.years[state.owner_agent_id]
                year.depreciation_deduction = checked_count(
                    year.depreciation_deduction + depreciation, "money addition"
                )
            property_.state = state.model_copy(
                update={
                    "owner_occupied_months": occupied_months,
                    "cumulative_depreciation": cumulative,
                    "depreciation_ytd": ytd,
                }
            )

    def reset_year(self) -> None:
        for property_ in self.properties.values():
            property_.state = property_.state.model_copy(update={"depreciation_ytd": 0})
