"""One rollout: financial books, tracked components, receipts and the monthly clock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Literal

from finance.augur.model.series import PrivateEquityEventKindCode
from finance.augur.sim import claims, observations, payments, private_equity, results
from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import (
    Action,
    Buy,
    ClaimId,
    Consume,
    Contribute,
    Liquidate,
    PayClaim,
    Sell,
    Transfer,
    Withdraw,
)
from finance.augur.sim.actor import MonthOpened
from finance.augur.sim.agent import EconomicAgent, Mail
from finance.augur.sim.bills import Bill, Biller
from finance.augur.sim.books import (
    AccountBalance,
    AccountRef,
    Book,
    CapitalGainState,
    IncomeState,
    JournalEntry,
    MortgageState,
    Posting,
    TaxPaymentOutcome,
    TlhPortfolioState,
)
from finance.augur.sim.compiler.income_sources import income_source_wire_id
from finance.augur.sim.distributions import Distributions
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.held_bonds import BondStatement, HeldBonds
from finance.augur.sim.holdings import Holdings, private_issuer
from finance.augur.sim.ids import AgentId
from finance.augur.sim.managed import ComponentEffects, ManagedPortfolios, TlhStatement
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, is_quantity_scale, position_value
from finance.augur.sim.mortgage import InstallmentPaid, Mortgage, MortgagePayment, ServicingStatement
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedAmount,
    PreparedBond,
    PreparedDistribution,
    PreparedFixedAmount,
    PreparedHoldingPool,
    PreparedIndexedAmount,
    PreparedIndexedCoupon,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedLot,
    PreparedObligation,
    PreparedPropertyCashflow,
    PreparedRecurringObligation,
    PreparedRecurringPropertyCashflow,
    PreparedRecurringTransfer,
    PreparedTlhPortfolio,
    PreparedTransfer,
    _MortgageInterestDeduction,
    _PropertyPurchase,
    _PropertyTax,
    _SaltDeduction,
    _TenderPolicy,
)
from finance.augur.sim.property import Housing, Properties, mortgage_terms
from finance.augur.sim.property_tax import PropertyTaxAuthority, PropertyTaxBill
from finance.augur.sim.scenario import InterestIncome, TransferIncomeCategory
from finance.augur.sim.tax_authority import Assessment, TaxAuthority
from finance.augur.sim.tlh import ModeledRealizations, TlhMarketUpdate, TlhOpening, TlhPortfolio

type Capture = Literal["summary", "dense", "forensic"]

_NO_REALIZATIONS = ModeledRealizations()


class World:
    """One rollout's present state and the clock; nothing here is a history.

    Track an agent and the contracts and bills that exist at month zero, `start`, then
    `step` until `finished`. When a month opens each counterparty is posted the
    statements it reads and `MonthOpened`, and what it demands becomes its payer's due;
    then every tracked agent receives its statements, dues and last month's receipts.
    `step` delivers `MonthOpened` to the agents and settles the actions they return.
    The batch session drives several worlds the same way. Component outcome lists
    hold this month only and are cleared when the next month opens; whoever wants a
    series reads state between steps.
    """

    def __init__(
        self,
        market: MarketPath,
        *,
        horizon_months: int,
        income_sources: Sequence[TransferIncomeCategory] = (),
        jurisdictions: Sequence[PreparedJurisdiction] = (),
    ) -> None:
        """An empty world on one market path; declare books and track actors before `start`.

        `income_sources` and `jurisdictions` are the tax vocabulary every taxpayer shares;
        an untaxed composition leaves both empty.
        """
        if horizon_months <= 0:
            raise ValueError("horizon must be positive")
        for series in market.series.values():
            if series.snapshots != horizon_months + 1:
                raise ValueError(
                    f"series {series.series_id!r} covers {series.snapshots} snapshots, "
                    f"not the horizon's {horizon_months + 1}"
                )
        self.market = market
        self.rollout_id = market.rollout_id
        self.horizon_months = horizon_months
        self.agents: list[EconomicAgent] = []
        self.specs: dict[str, PreparedTlhPortfolio] = {}
        self.portfolios: dict[str, TlhPortfolio] = {}
        self.income_sources = tuple(income_sources)
        self.jurisdictions = tuple(jurisdictions)
        self.accounting = Accounting(income_sources, jurisdictions)
        self.holdings = Holdings()
        # Domains nothing declared, tracked or attached are absent, not empty.
        self.managed: ManagedPortfolios | None = None
        self.properties: Properties | None = None
        self.bonds: HeldBonds | None = None
        self.distributions: Distributions | None = None
        self.private_equity: private_equity.PrivateEquity | None = None
        # Standing cashflows, moved when their month opens; see `declare_flow`.
        self._scheduled_transfers: tuple[PreparedTransfer, ...] = ()
        self._recurring_transfers: tuple[PreparedRecurringTransfer, ...] = ()
        self._scheduled_property_cashflows: tuple[PreparedPropertyCashflow, ...] = ()
        self._recurring_property_cashflows: tuple[PreparedRecurringPropertyCashflow, ...] = ()
        self.claims = claims.Claims(0, [])
        # Counterparties, in the order their demands are registered.
        self.billers: list[Biller] = []
        self.property_tax_authorities: list[PropertyTaxAuthority] = []
        self.tax_authorities: list[TaxAuthority] = []
        self.month = 0
        self.failed_month: int | None = None
        # This month's settlement outcomes, cleared when the next month opens.
        self.obligations: list[payments.ObligationOutcome] = []
        self.payments: list[results.Payment] = []
        self.mortgages: dict[str, Mortgage] = {}
        self.mortgage_payments: dict[str, MortgagePayment] = {}
        self.previous_receipts: list[results.Receipt] = []
        self.stop: results.Stop | None = None
        self.failed = False
        self.shortfall = 0  # The configured runner's grouped-settlement shortfall for this month.
        self.started = False
        self.opened = False
        self.finished = False

    def _composing(self) -> None:
        if self.started:
            raise ValueError("declare and track components before starting the world")

    def declare_account(self, account: PreparedAccount) -> None:
        self._composing()
        self.accounting.declare(account)

    def declare_pool(self, pool: PreparedHoldingPool) -> None:
        """A place the owner may hold a security; a public one is priceable at every snapshot.

        A pool whose quote is zero would value the position at nothing rather than admit the
        engine cannot price it, so the declaration refuses it. Only a portfolio the owner holds
        exclusively through a manager may carry a zero mark.
        """
        self._composing()
        if not is_quantity_scale(pool.quantity_scale):
            raise ValueError("invalid holding pool quantity scale")
        slot = (pool.agent_id, pool.account_id, pool.asset_id)
        if any((declared.agent_id, declared.account_id, declared.asset_id) == slot for declared in self.holdings.pools):
            raise ValueError("duplicate holding pool declaration")
        if slot in self.holdings.managed:
            raise ValueError(f"TLH pool {slot!r} must have exactly one component owner and no ordinary holdings")
        if private_issuer(pool.asset_id) is None:
            if f"security:{pool.asset_id}" not in self.market.series:
                raise ValueError(f"missing public security series for {pool.asset_id!r}")
            self.market.require_prices(f"security:{pool.asset_id}")
        self.holdings.declare_pool(self.accounting, pool)

    def hold(self, holding: PreparedLot | PreparedBond) -> None:
        """A lot or dated bond held at month zero."""
        self._composing()
        if isinstance(holding, PreparedLot):
            pool = next(
                (
                    pool
                    for pool in self.holdings.pools
                    if (pool.agent_id, pool.account_id, pool.asset_id)
                    == (holding.agent_id, holding.account_id, holding.asset_id)
                ),
                None,
            )
            if pool is None:
                raise ValueError(f"lot {holding.lot_id!r} references no declared holding pool")
            if pool.quantity_scale != holding.quantity_scale:
                raise ValueError(f"lot {holding.lot_id!r} has a mixed quantity scale")
            bought = (holding.agent_id, holding.account_id, holding.asset_id, holding.purchase_month)
            for held in self.holdings.lots:
                if (held.spec.agent_id, held.spec.account_id, held.spec.asset_id, held.spec.purchase_month) == bought:
                    raise ValueError(
                        f"lots {held.spec.lot_id!r} and {holding.lot_id!r} share {holding.purchase_month=} in one "
                        "pool; FIFO sells oldest first, so their order would rest on lot ids alone"
                    )
            if (issuer := private_issuer(holding.asset_id)) is not None:
                self._check_issuer(issuer)
                if self.private_equity is None:
                    self.private_equity = private_equity.PrivateEquity([])
            self.holdings.hold(self.accounting, holding)
            return
        self._check_bond(holding)
        if self.bonds is None:
            self.bonds = HeldBonds((), self.market)
        self.bonds.hold(holding)

    def _check_issuer(self, issuer: str) -> None:
        """Every protocol channel on the path and in its range, a tender exactly where an opportunity is."""
        for channel, (minimum, maximum) in private_equity.CHANNEL_RANGES.items():
            if f"private_equity_{channel}:{issuer}" not in self.market.series:
                raise ValueError(f"missing private-equity {channel} series for issuer {issuer!r}")
            # The terminal snapshot is not simulated but it is read: terminal value comes from it.
            for month, value in enumerate(self.market.path(f"private_equity_{channel}:{issuer}")):
                if not minimum <= value <= maximum:
                    raise ValueError(f"issuer {issuer!r} has invalid {channel} value {value} at month {month}")
        if any(
            (event == PrivateEquityEventKindCode.TENDER) != (active == 1)
            for event, active in zip(
                self.market.path(f"private_equity_event_kind:{issuer}"),
                self.market.path(f"private_equity_sale_opportunity:{issuer}"),
                strict=True,
            )
        ):
            raise ValueError(f"issuer {issuer!r} has a tender event and a sale opportunity in different months")

    def _check_bond(self, bond: PreparedBond) -> None:
        """Bought at par, a nonnegative coupon on whole periods, and indexed only on an index the path carries."""
        if AccountRef(agent_id=bond.agent_id, account_id=bond.account_id) not in self.accounting.declared:
            raise ValueError(f"bond {bond.bond_id!r} references an unknown account")
        if bond.issuer_jurisdiction_id is not None and bond.issuer_jurisdiction_id not in {
            item.jurisdiction_id for item in self.jurisdictions
        }:
            raise ValueError(f"bond {bond.bond_id!r} has unknown issuer")
        term = bond.maturity_month_index - bond.purchase_month_index
        coupon = bond.coupon.amount if isinstance(bond.coupon, PreparedFixedAmount) else bond.coupon.annual_rate_ppb
        if (
            bond.face_value <= 0
            or bond.purchase_price != bond.face_value
            or coupon < 0
            or bond.coupon_period_months <= 0
            or term <= 0
            or term % bond.coupon_period_months
        ):
            raise ValueError(f"invalid bond terms for {bond.bond_id!r}")
        if isinstance(bond.coupon, PreparedIndexedCoupon):
            if "inflation" not in self.market.series:
                raise ValueError(f"missing inflation series for indexed bond {bond.bond_id!r}")
            if max(0, bond.purchase_month_index) > self.horizon_months or any(
                value <= 0 for value in self.market.path("inflation")
            ):
                raise ValueError(f"invalid bond inflation path for {bond.bond_id!r}")

    def declare_tender_policy(self, policy: _TenderPolicy) -> None:
        """How an owner answers its issuers' sale opportunities and where compulsory proceeds land."""
        self._composing()
        if (
            AccountRef(agent_id=policy.owner_agent_id, account_id=policy.proceeds_account_id)
            not in self.accounting.declared
        ):
            raise ValueError("tender proceeds account is not declared")
        if self.private_equity is None:
            self.private_equity = private_equity.PrivateEquity([])
        self.private_equity.tender_policies.append(policy)

    def declare_distribution(self, spec: PreparedDistribution) -> None:
        """A security's periodic payout to its holder, on the path's distribution series.

        The payout goes to whoever holds the security, so the owner needs a declared pool or a
        managed portfolio for it — a cash account is not a place a security is held.
        """
        self._composing()
        if f"security_distribution:{spec.asset_id}" not in self.market.series:
            raise ValueError(f"missing distribution series for {spec.asset_id!r}")
        for month, rate in enumerate(self.market.path(f"security_distribution:{spec.asset_id}")):
            if rate < 0:
                raise ValueError(f"series for {spec.asset_id!r} has a negative security distribution at month {month}")
        if AccountRef(agent_id=spec.agent_id, account_id=spec.to_account_id) not in self.accounting.declared:
            raise ValueError("distribution references an unknown account")
        holding = (spec.agent_id, spec.holding_account_id, spec.asset_id)
        if self.distributions is not None and holding in {
            (declared.agent_id, declared.holding_account_id, declared.asset_id) for declared in self.distributions.specs
        }:
            raise ValueError(f"duplicate distribution for {':'.join(holding)}: the holding would pay out twice")
        if (
            holding
            not in {(pool.agent_id, pool.account_id, pool.asset_id) for pool in self.holdings.pools}
            | self.holdings.managed
        ):
            raise ValueError(f"distribution references no lots for {':'.join(holding)}")
        if (
            not spec.tax_character
            or any(not 0 <= part.fraction_ppb <= MONEY_FACTOR_SCALE for part in spec.tax_character)
            or sum(part.fraction_ppb for part in spec.tax_character) != MONEY_FACTOR_SCALE
        ):
            raise ValueError("invalid distribution tax character split")
        for part in spec.tax_character:
            if part.issuer_jurisdiction_id is not None and part.issuer_jurisdiction_id not in {
                item.jurisdiction_id for item in self.jurisdictions
            }:
                raise ValueError("distribution has unknown issuer")
            if InterestIncome(issuer_jurisdiction_id=part.issuer_jurisdiction_id) not in self.income_sources:
                raise ValueError("distribution has undeclared income source")
        if self.distributions is None:
            self.distributions = Distributions(
                (), {(spec.owner_agent_id, spec.account_id, spec.asset_id) for spec in self.specs.values()}
            )
        self.distributions.specs = (*self.distributions.specs, spec)

    def declare_portfolio(self, spec: PreparedTlhPortfolio) -> None:
        """A managed TLH portfolio held at month zero, marked at the path's opening price."""
        self._composing()
        if spec.portfolio_id in self.specs:
            raise ValueError(f"duplicate TLH portfolio {spec.portfolio_id!r}")
        if not any(account.agent_id == spec.owner_agent_id for account in self.accounting.declared):
            raise ValueError(f"TLH portfolio {spec.portfolio_id!r} has an unknown owner")
        slot = (spec.owner_agent_id, spec.account_id, spec.asset_id)
        # The manager settles the slot's distributions, so an ordinary lot beside it would silently earn none.
        if slot in self.holdings.managed or any(
            (pool.agent_id, pool.account_id, pool.asset_id) == slot for pool in self.holdings.pools
        ):
            raise ValueError(f"TLH pool {slot!r} must have exactly one component owner and no ordinary holdings")
        if f"security:{spec.asset_id}" not in self.market.series:
            raise ValueError(f"missing security series for TLH portfolio {spec.portfolio_id!r}")
        # A managed index may be marked at zero (see `declare_pool`), never below it.
        for month, price in enumerate(self.market.path(f"security:{spec.asset_id}")):
            if price < 0:
                raise ValueError(
                    f"TLH portfolio {spec.portfolio_id!r}: index price must be nonnegative, got {price} at month {month}"
                )
        portfolio = TlhPortfolio(
            spec.assumptions,
            TlhOpening(month=-1, price=self.market.value(f"security:{spec.asset_id}", 0), cohorts=spec.initial_cohorts),
        )
        if self.managed is None:
            self.managed = ManagedPortfolios(self.income_sources, self.jurisdictions)
        self.managed.open(self.accounting, spec, self.statement(spec, portfolio, 0))
        self.holdings.reserve(*slot)
        if self.distributions is not None:
            self.distributions.managed_slots.add(slot)
        self.specs[spec.portfolio_id] = spec
        self.portfolios[spec.portfolio_id] = portfolio

    def managed_portfolios(self) -> ManagedPortfolios:
        """The managed-portfolio component, for a caller settling into one; raises when none is declared."""
        if self.managed is None:
            raise ValueError("no managed portfolio is declared")
        return self.managed

    def _purchases(self) -> tuple[_PropertyPurchase, ...]:
        return () if self.properties is None else self.properties.housing.purchases

    def declare_housing(
        self, housing: Housing, tax_policies: Sequence[_PropertyTax] = (), locations: Sequence[PreparedLocation] = ()
    ) -> None:
        """Properties bought on a scripted schedule (month zero for one held from the start), their scripted
        lifecycle, and the authorities that tax them. Declared once per world."""
        self._composing()
        if self.properties is not None:
            raise ValueError("housing is already declared")
        housing.check(self.horizon_months)
        purchases = {purchase.property_id: purchase for purchase in housing.purchases}
        located = {location.location_id: location for location in locations}
        for liability_id in self.mortgages:
            if any(
                purchase.mortgage is not None and purchase.mortgage.liability_id == liability_id
                for purchase in housing.purchases
            ):
                raise ValueError(f"duplicate mortgage liability {liability_id!r}")
        for agent_id in {residence.agent_id for residence in housing.initial_residences} | {
            change.agent_id for change in housing.residence_events
        }:
            if not any(account.agent_id == agent_id for account in self.accounting.declared):
                raise ValueError(f"primary residence names unknown agent {agent_id!r}")
        for purchase in housing.purchases:
            if purchase.location_id not in located:
                known = ", ".join(repr(id_) for id_ in sorted(located)) or "<none>"
                raise ValueError(
                    f"scheduled property purchase {purchase.cause_id!r} references unknown location_id "
                    f"{purchase.location_id!r}; known location ids: {known}"
                )
            for account in (
                AccountRef(agent_id=purchase.buyer_agent_id, account_id=purchase.buyer_account_id),
                AccountRef(agent_id=purchase.seller_agent_id, account_id=purchase.seller_account_id),
            ):
                if account not in self.accounting.declared:
                    raise ValueError(f"purchase {purchase.cause_id!r} references an unknown account")
            principal = 0 if purchase.mortgage is None else purchase.mortgage.principal
            if (
                purchase.purchase_price <= 0
                or purchase.down_payment < 0
                or purchase.buyer_closing_cost < 0
                or not 0 <= purchase.rented_fraction_ppb <= MONEY_FACTOR_SCALE
                or not 0 <= purchase.land_value_fraction_ppb <= MONEY_FACTOR_SCALE
                or purchase.down_payment + principal != purchase.purchase_price
            ):
                raise ValueError(f"invalid property terms for {purchase.property_id!r}")
        for sale in housing.sales:
            if sale.property_id not in purchases:
                raise ValueError(f"sale references unknown property {sale.property_id!r}")
            # A sale prices the property off its location's path; nothing else reads that series.
            series_id = f"home_value:{purchases[sale.property_id].location_id}"
            if series_id not in self.market.series:
                raise ValueError(f'missing series "{series_id}"')
        # A held property is marked off its location's path wherever the path carries one.
        for series_id in {f"home_value:{purchase.location_id}" for purchase in housing.purchases}:
            if series_id in self.market.series:
                self.market.require_prices(series_id)
        taxed: dict[tuple[str, int], int] = {}
        for index, policy in enumerate(tax_policies):
            owned = purchases.get(policy.property_id)
            if owned is None:
                raise ValueError(f"property tax policy references unknown property {policy.property_id!r}")
            if policy.owner_agent_id != owned.buyer_agent_id:
                raise ValueError(f"property tax policy for {policy.property_id!r} is not owed by the property's buyer")
            if policy.end_month is not None and policy.end_month < policy.start_month:
                raise ValueError(f"property tax policy for {policy.property_id!r} ends before it starts")
            last = (
                self.horizon_months - 1 if policy.end_month is None else min(policy.end_month, self.horizon_months - 1)
            )
            for month in range(max(policy.start_month, 0), last + 1):
                if (previous := taxed.setdefault((policy.property_id, month), index)) != index:
                    raise ValueError(
                        f"overlapping property tax policies for {policy.property_id!r} at month {month}: "
                        f"indexes {previous} and {index}"
                    )
        self.properties = Properties(housing, self.accounting)
        self.property_tax_authorities = [
            PropertyTaxAuthority(
                policy, purchases[policy.property_id], located[purchases[policy.property_id].location_id]
            )
            for policy in tax_policies
        ]

    def declare_flow(self, flow: PreparedTransfer | PreparedRecurringTransfer) -> None:
        """A standing cashflow between declared accounts, moved when its month opens.

        A property's cashflow moves only while that property is held, so housing is declared first.
        """
        self._composing()
        label = f"cashflow {flow.cause_id!r}"
        for account in (flow.from_account, flow.to_account):
            if account not in self.accounting.declared:
                raise ValueError(f"{label} names unknown declared account {account.agent_id}:{account.account_id}")
        if flow.income_category is not None and flow.income_category not in self.income_sources:
            raise ValueError(f"{label} has undeclared income source {flow.income_category!r}")
        if isinstance(flow, PreparedPropertyCashflow | PreparedRecurringPropertyCashflow) and flow.property_id not in {
            purchase.property_id for purchase in self._purchases()
        }:
            raise ValueError(f"{label} references undeclared property {flow.property_id!r}")
        self._check_amount(label, flow.amount, self._due_months(label, flow))
        match flow:
            case PreparedPropertyCashflow():
                self._scheduled_property_cashflows = (*self._scheduled_property_cashflows, flow)
            case PreparedRecurringPropertyCashflow():
                self._recurring_property_cashflows = (*self._recurring_property_cashflows, flow)
            case PreparedTransfer():
                self._scheduled_transfers = (*self._scheduled_transfers, flow)
            case PreparedRecurringTransfer():
                self._recurring_transfers = (*self._recurring_transfers, flow)

    def declare_deduction(self, policy: _MortgageInterestDeduction | _SaltDeduction) -> None:
        """An itemized deduction an enrolled taxpayer claims when its tax year closes."""
        self._composing()
        tax = self.accounting.tax
        if isinstance(policy, _MortgageInterestDeduction):
            if policy.owner_agent_id not in tax.years:
                raise ValueError(f"mortgage interest deduction for {policy.owner_agent_id!r} names no taxpayer")
            tax.mortgage_interest_policies = (*tax.mortgage_interest_policies, policy)
        else:
            if policy.profile_id not in tax.years:
                raise ValueError(f"SALT deduction for {policy.profile_id!r} names no taxpayer")
            tax.salt_policies = (*tax.salt_policies, policy)

    def _due_months(
        self,
        label: str,
        schedule: PreparedTransfer | PreparedRecurringTransfer | PreparedObligation | PreparedRecurringObligation,
    ) -> range:
        """The horizon months a schedule is due in; a one-off month must fall inside the horizon."""
        if isinstance(schedule, PreparedTransfer | PreparedObligation):
            if not 0 <= schedule.month < self.horizon_months:
                raise ValueError(f"{label} has month {schedule.month}, outside the horizon [0, {self.horizon_months})")
            return range(schedule.month, schedule.month + 1)
        if schedule.end_month is not None and schedule.end_month < schedule.start_month:
            raise ValueError(f"{label} has end month {schedule.end_month} before start month {schedule.start_month}")
        end = self.horizon_months if schedule.end_month is None else min(schedule.end_month + 1, self.horizon_months)
        return range(max(schedule.start_month, 0), end)

    def _check_amount(self, label: str, amount: PreparedAmount, months: range) -> None:
        """An indexed amount due in `months` reads its series at its base month, which none precedes."""
        if not isinstance(amount, PreparedIndexedAmount) or not months:
            return
        if months[0] < amount.base_month_index:
            raise ValueError(
                f"series-indexed amount {label} is active at month {months[0]} "
                f"before base month {amount.base_month_index}"
            )
        if amount.series_id not in self.market.series:
            raise ValueError(f"series-indexed amount {label} references missing series {amount.series_id!r}")
        if self.market.value(amount.series_id, amount.base_month_index) == 0:
            raise ValueError(
                f"series {amount.series_id!r} has zero base level at month {amount.base_month_index} for {label}"
            )

    def statement(
        self, spec: PreparedTlhPortfolio, portfolio: TlhPortfolio, month: int
    ) -> observations.TlhPortfolioObservation:
        """`portfolio` marked at `month`'s index level, which also decides whether it takes a contribution."""
        price = self.market.value(f"security:{spec.asset_id}", month)
        value = portfolio._observe_at_price(price)
        return observations.TlhPortfolioObservation(
            portfolio_id=spec.portfolio_id,
            owner_agent_id=spec.owner_agent_id,
            account_id=spec.account_id,
            asset_id=spec.asset_id,
            value=value.value,
            reported_tax_basis=value.reported_tax_basis,
            accepts_contributions=price > 0,
        )

    def effects(
        self,
        spec: PreparedTlhPortfolio,
        candidate: TlhPortfolio,
        cash_account_id: str | None,
        cash_amount: int,
        realizations: ModeledRealizations = _NO_REALIZATIONS,
    ) -> ComponentEffects:
        return ComponentEffects(
            observation=self.statement(spec, candidate, self.month),
            cash_account_id=cash_account_id,
            cash_amount=cash_amount,
            short_term_gain=realizations.short_term_gain,
            long_term_gain=realizations.long_term_gain,
        )

    def track(self, actor: EconomicAgent | Mortgage | Biller | TaxAuthority) -> None:
        """Register an agent, a month-zero contract, a bill or a tax authority; prepared input is checked against an agent."""
        if isinstance(actor, Mortgage):
            self._track_mortgage(actor)
            return
        if isinstance(actor, Biller):
            self._track_biller(actor)
            return
        if isinstance(actor, TaxAuthority):
            self._composing()
            self.accounting.enroll(actor.profile)
            self.tax_authorities.append(actor)
            return
        if not isinstance(actor, EconomicAgent):
            raise TypeError(
                "only EconomicAgent subclasses, Mortgage contracts, Billers and TaxAuthorities can be tracked"
            )
        self._track(actor)

    def _track_biller(self, biller: Biller) -> None:
        if self.started:
            raise ValueError("track components before starting the world")
        spec = biller.spec
        if spec.property_id is not None and spec.property_id not in {
            purchase.property_id for purchase in self._purchases()
        }:
            raise ValueError("a bill attached to a property needs that property declared first")
        self.validate_scope(spec.from_account.agent_id)
        if spec.from_account not in self.accounting.declared:
            raise ValueError("bill payer account is not declared")
        label = f"obligation {spec.obligation_id!r}"
        self._check_amount(label, spec.amount_due, self._due_months(label, spec))
        self.billers.append(biller)

    def _track_mortgage(self, mortgage: Mortgage) -> None:
        """Open the ledger with the contract's outstanding balance; servicing then runs on the world's clock."""
        if self.started:
            raise ValueError("track components before starting the world")
        terms = mortgage.terms
        if mortgage.opening_principal is None:
            raise ValueError("a tracked mortgage needs the principal outstanding at month zero")
        if terms.liability_id in self.mortgages or any(
            purchase.mortgage is not None and purchase.mortgage.liability_id == terms.liability_id
            for purchase in self._purchases()
        ):
            raise ValueError(f"duplicate mortgage liability {terms.liability_id!r}")
        self.validate_scope(terms.borrower.agent_id)
        if terms.borrower not in self.accounting.declared:
            raise ValueError("mortgage borrower account is not declared")
        liability = AccountRef(agent_id=terms.borrower.agent_id, account_id=f"liability:mortgage:{terms.liability_id}")
        receivable = AccountRef(
            agent_id=terms.lender.agent_id, account_id=f"asset:mortgage-receivable:{terms.liability_id}"
        )
        borrower_equity = AccountRef(agent_id=terms.borrower.agent_id, account_id="equity:opening")
        lender_equity = AccountRef(agent_id=terms.lender.agent_id, account_id="equity:opening")
        for account in (
            liability,
            receivable,
            borrower_equity,
            lender_equity,
            AccountRef(agent_id=terms.borrower.agent_id, account_id=f"expense:mortgage-interest:{terms.liability_id}"),
            AccountRef(agent_id=terms.lender.agent_id, account_id=f"income:mortgage-interest:{terms.liability_id}"),
        ):
            self.accounting.ledger.ensure_account(account)
        owed = checked_count(-mortgage.opening_principal, "money negation")
        self.accounting.apply(
            JournalEntry(
                month=0,
                cause_id=f"opening:mortgage:{terms.liability_id}",
                postings=[
                    Posting(account=liability, amount=owed),
                    Posting(account=borrower_equity, amount=mortgage.opening_principal),
                    Posting(account=receivable, amount=mortgage.opening_principal),
                    Posting(account=lender_equity, amount=owed),
                ],
            )
        )
        self.mortgages[terms.liability_id] = mortgage

    def _track(self, agent: EconomicAgent) -> None:
        if self.started:
            raise ValueError("track components before starting the world")
        if self.agents:
            raise ValueError("one decision-making agent per world until multi-actor sequencing is decided")
        self.validate_scope(agent.agent_id)
        self.agents.append(agent)

    @property
    def mark_month(self) -> int:
        """The month whose prices value the current books: a stopped world keeps its stop month."""
        return self.month if self.failed_month is None else self.failed_month

    def start(self) -> None:
        if self.started:
            raise ValueError("invalid world lifecycle state: already started")
        self.started = True
        self.open_month()

    def step(self) -> None:
        """One month: each tracked agent acts once on `MonthOpened`, actions execute in order, the month closes."""
        if not self.started or self.finished:
            raise ValueError("world is not running")
        if not self.agents:
            raise ValueError("step needs a tracked agent; scripted-only paths run through the batch session")
        if not self.opened:
            self.open_month()
        for agent in self.agents:
            actions = agent.handle(MonthOpened(month=self.month))
            self.begin_actions(actions)
            for action in actions:
                if isinstance(self.execute(agent.agent_id, action).outcome, results.Rejected):
                    break
        self.close_month()

    def open_month(self) -> None:
        """Clear last month's outcomes, advance components, raise this month's claims, then post the mail."""
        if self.finished:
            raise ValueError("the world is finished")
        if self.opened:
            raise ValueError("the month is already open")
        if self.month:
            for component in (
                self.accounting,
                self.holdings,
                self.managed,
                self.properties,
                self.bonds,
                self.distributions,
                self.private_equity,
            ):
                if component is not None:
                    component.begin_month()
            self.obligations.clear()
            self.payments.clear()
            self.shortfall = 0
        self.opened = True
        distributions = () if self.distributions is None else self.distributions.specs
        for spec in self.specs.values():
            current = self.portfolios[spec.portfolio_id]
            for distribution in distributions:
                if (distribution.agent_id, distribution.holding_account_id, distribution.asset_id) != (
                    spec.owner_agent_id,
                    spec.account_id,
                    spec.asset_id,
                ):
                    continue
                rate = self.market.value(f"security_distribution:{spec.asset_id}", self.month)
                self.managed_portfolios().distribute(
                    self.accounting, self.month, distribution, current.distribution(rate)
                )
            candidate = deepcopy(current)
            realized = candidate.advance(
                TlhMarketUpdate(self.month, self.market.value(f"security:{spec.asset_id}", self.month))
            )
            self.managed_portfolios().settle(
                self.accounting,
                self.month,
                spec.owner_agent_id,
                f"tlh:{spec.portfolio_id}:advance:m{self.month}",
                self.effects(spec, candidate, None, 0, realized),
                operation="modeled_realization",
            )
            self.portfolios[spec.portfolio_id] = candidate
        self.open_mortgages()
        for agent in self.agents:
            for message in self.open_mail(agent.agent_id):
                if agent.handle(message):
                    raise ValueError("statements and dues take no reply; act on MonthOpened")

    def open_mortgages(self) -> None:
        """Originate/pay off configured contracts, then post each active contract its servicing mail and collect its quote."""
        candidates = {}
        for purchase in self._purchases():
            financing = purchase.mortgage
            if purchase.month != self.month or financing is None:
                continue
            candidates[financing.liability_id] = Mortgage(mortgage_terms(purchase))
        originated, paid_off = self.prepare_month(self.month, candidates, self.mortgages)
        for id_ in paid_off:
            self.mortgages[id_].payoff()
        for id_ in originated:
            self.mortgages[id_] = candidates[id_]
        self.mortgage_payments = {}
        for id_, loan in self.mortgages.items():
            if not loan.active:
                continue
            statement = ServicingStatement(
                month=self.month,
                principal=self.mortgage_principal(id_),
                rented_fraction_ppb=self.property_rented_fraction(loan.terms.property_id),
            )
            if loan.handle(statement):
                raise ValueError("a servicing statement takes no reply")
            for quote in loan.handle(MonthOpened(month=self.month)):
                self.mortgage_payments[id_] = quote
        self.assemble_claims(list(self.mortgage_payments.values()))

    def mortgage_snapshots(self) -> list[MortgageState]:
        return [loan.observe(self.mortgage_principal(id_)) for id_, loan in self.mortgages.items()]

    def check_claims(self, actions: Sequence[Action]) -> None:
        for action in actions:
            if isinstance(action, PayClaim) and not action.claim._belongs_to(self, self.rollout_id):
                raise ValueError("claim belongs to a different rollout or session")

    def begin_actions(self, actions: Sequence[Action]) -> None:
        """Validate the month's claim handles before any action executes; last month's receipts are consumed."""
        self.check_claims(actions)
        self.previous_receipts = []

    def execute(self, actor: str, action: Action) -> results.Receipt:
        """Execute one action on behalf of `actor`; a rejection stops this path."""
        if self.failed:
            raise ValueError("cannot act on a stopped rollout")
        if not self.opened:
            raise ValueError("open the month before acting")
        index = len(self.previous_receipts)
        if isinstance(action, Contribute | Withdraw | Liquidate):
            outcome = self._component_action(actor, action)
        else:
            outcome = self.apply(actor, action, index)
        historical_action = action
        if isinstance(action, PayClaim):
            historical_action = action.model_copy(
                update={"claim": ClaimId(month=action.claim.month, index=action.claim.index)}
            )
        receipt = results.Receipt(month=self.month, action_index=index, action=historical_action, outcome=outcome)
        self.previous_receipts.append(receipt)
        if isinstance(outcome, results.Rejected):
            self.failed = True
            self.stop = results.RejectedAction(month=self.month, action_index=index)
        return receipt

    def _component_action(
        self, actor: str, action: Contribute | Withdraw | Liquidate
    ) -> results.Executed | results.Rejected:
        def reject(detail: str) -> results.Rejected:
            return results.Rejected(reason=results.InvalidRequest(detail=detail))

        spec = self.specs.get(action.portfolio_id)
        if spec is None or action.agent_id != spec.owner_agent_id or actor != action.agent_id:
            return reject("unknown or unowned TLH portfolio")
        if not action.cause_id:
            return reject("TLH cause identifier must not be empty")
        available = self.account_balance(actor, action.cash_account_id)
        if available is None:
            return reject("unknown TLH cash account")
        current = self.portfolios[spec.portfolio_id]
        if isinstance(action, Contribute | Withdraw):
            if action.amount < 0:
                return reject("TLH amount must be nonnegative")
            if isinstance(action, Contribute) and action.amount > available:
                return reject("TLH contribution exceeds available cash")
            if isinstance(action, Withdraw) and action.amount > current.observe().value:
                return reject("TLH withdrawal exceeds portfolio value")
            if (
                isinstance(action, Contribute)
                and action.amount
                and not self.market.value(f"security:{spec.asset_id}", self.month)
            ):
                return reject("a worthless index takes no TLH contribution")
        candidate = deepcopy(current)
        if isinstance(action, Contribute):
            candidate.contribute(action.amount)
            effects = self.effects(spec, candidate, action.cash_account_id, -action.amount)
        else:
            withdrawal = candidate.withdraw(action.amount) if isinstance(action, Withdraw) else candidate.liquidate()
            effects = self.effects(
                spec, candidate, action.cash_account_id, withdrawal.cash_received, withdrawal.realizations
            )
        managed = self.managed_portfolios()
        managed.validate_request(action, effects)
        managed.settle(
            self.accounting,
            self.month,
            actor,
            action.cause_id,
            effects,
            operation="contribution" if isinstance(action, Contribute) else "redemption",
        )
        self.portfolios[spec.portfolio_id] = candidate
        return results.Executed()

    def close_month(self) -> None:
        """Stop on a tracked agent's unpaid claims, mark components, close the books; the next month opens lazily."""
        if not self.opened:
            raise ValueError("the month is not open")
        for agent in self.agents:
            unpaid = self.unpaid_claims(agent.agent_id)
            self.shortfall = sum(claim.amount_due for claim in unpaid)
            if unpaid and self.stop is None:
                self.failed = True
                self.stop = results.UnpaidClaims(month=self.month, claims=[claim.id for claim in unpaid])
        if not self.failed and self.private_equity is not None:
            self.private_equity.advance(self.accounting, self.holdings, self.market, self.marks(), self.month)
        marks = [
            self.statement(spec, self.portfolios[spec.portfolio_id], self.month + (not self.failed))
            for spec in self.specs.values()
        ]
        if self.managed is not None:
            self.managed.mark(marks)
        for id_ in (
            claim.effect.terms.liability_id
            for claim in self.claims.entries
            if claim.paid and isinstance(claim.effect, MortgagePayment)
        ):
            self.mortgages[id_].handle(
                InstallmentPaid(payment=self.mortgage_payments[id_], principal_after=self.mortgage_principal(id_))
            )
        reset_year = not self.failed and (self.month + 1) % 12 == 0
        self.close_books(failed=self.failed, mortgages=list(self.mortgages.values()))
        if reset_year:
            for loan in self.mortgages.values():
                loan.reset_year()
        self.opened = False
        self.finished = self.failed or self.month == self.horizon_months

    def validate_scope(self, actor: str) -> None:
        if not any(account.agent_id == actor for account in self.accounting.declared):
            raise ValueError(f"unknown actor {actor!r}")
        for pool in self.holdings.pools:
            if (
                pool.agent_id == actor
                and private_issuer(pool.asset_id) is None
                and f"security:{pool.asset_id}" not in self.market.series
            ):
                raise ValueError(f"missing public security series for {pool.asset_id!r}")

    def account_balance(self, actor: str, account: str) -> int | None:
        key = AccountRef(agent_id=actor, account_id=account)
        return self.accounting.ledger.balance(key) if key in self.accounting.declared else None

    def mortgage_principal(self, liability_id: str) -> int:
        """Outstanding principal from the ledger, for a tracked contract or a configured purchase's financing."""
        loan = self.mortgages.get(liability_id)
        if loan is not None:
            borrower = loan.terms.borrower.agent_id
        else:
            purchase = next(
                (
                    purchase
                    for purchase in self._purchases()
                    if purchase.mortgage is not None and purchase.mortgage.liability_id == liability_id
                ),
                None,
            )
            if purchase is None:
                raise ValueError("unknown mortgage liability")
            borrower = purchase.buyer_agent_id
        return checked_count(
            -self.accounting.ledger.balance(
                AccountRef(agent_id=borrower, account_id=f"liability:mortgage:{liability_id}")
            ),
            "money negation",
        )

    def property_rented_fraction(self, property_id: str) -> int:
        # A tracked contract's property is not a component yet, so none of it is rented out.
        property_ = None if self.properties is None else self.properties.properties.get(property_id)
        return 0 if property_ is None else property_.state.rented_fraction_ppb

    def prepare_month(
        self, month: int, originations: Mapping[str, Mortgage], mortgages: Mapping[str, Mortgage]
    ) -> tuple[list[str], list[str]]:
        if month != self.month or month >= self.horizon_months:
            raise ValueError("invalid financial month")
        self.claims = claims.Claims(month, [])
        paid_off: list[str] = []
        originated: list[str] = []
        active: set[str] = set()
        if self.properties is not None:
            self.properties.assign_residences(month)
            paid_off = self.properties.lifecycle(self.accounting, self.market, month, mortgages)
        if self.bonds is not None:
            self.bonds.advance(self.accounting, month)
        if self.distributions is not None:
            self.distributions.advance(self.accounting, self.holdings, self.market, month)
        if self.properties is not None:
            originated = self.properties.purchase(self.accounting, month, originations)
            active = {row.property_id for row in self.properties.snapshots() if row.active}
        flows = [flow for flow in self._scheduled_transfers if flow.month == month]
        recurring = [
            flow
            for flow in self._recurring_transfers
            if flow.start_month <= month and (flow.end_month is None or month <= flow.end_month)
        ]
        property_flows = [
            flow for flow in self._scheduled_property_cashflows if flow.month == month and flow.property_id in active
        ]
        property_recurring = [
            flow
            for flow in self._recurring_property_cashflows
            if flow.start_month <= month
            and (flow.end_month is None or month <= flow.end_month)
            and flow.property_id in active
        ]
        for flow in (*flows, *recurring, *property_flows, *property_recurring):
            self.accounting.transfer(
                month,
                Transfer(
                    cause_id=flow.cause_id,
                    from_account=flow.from_account,
                    to_account=flow.to_account,
                    amount=self.market.amount(flow.amount, month),
                ),
                actor=None,
                income=flow.income_category,
                deduction=flow.deduction_category,
            )
        return originated, paid_off

    def assemble_claims(self, installments: Sequence[MortgagePayment]) -> None:
        """Post each counterparty what it reads and `MonthOpened`; register its demands and the installments, in tier order."""
        month = self.month
        seen = set()
        for installment in installments:
            id_ = installment.terms.liability_id
            if (
                id_ in seen
                or installment.terms.origination_month >= month
                or not 0 <= installment.principal <= self.mortgage_principal(id_)
                or self.mortgage_principal(id_) <= 0
                or not 0 <= installment.rental_interest <= installment.interest
            ):
                raise ValueError("invalid mortgage installment")
            seen.add(id_)
        self.claims = claims.Claims(month, [])
        opened = MonthOpened(month=month)
        for biller in self.billers:
            property_id = biller.spec.property_id
            statement = (
                None
                if property_id is None or self.properties is None
                else self.properties.statement(property_id, month)
            )
            if statement is not None and biller.handle(statement):
                raise ValueError("a property statement takes no reply")
            for bill in biller.handle(opened):
                self.register(bill)
        for installment in installments:
            self.register(installment)
        for property_authority in self.property_tax_authorities:
            statement = (
                None
                if self.properties is None
                else self.properties.statement(property_authority.policy.property_id, month)
            )
            if statement is not None and property_authority.handle(statement):
                raise ValueError("a property statement takes no reply")
            for property_bill in property_authority.handle(opened):
                self.register(property_bill)
        for authority in self.tax_authorities:
            if authority.handle(self.accounting.liability_statement(month)):
                raise ValueError("a liability statement takes no reply")
            for assessment in authority.handle(opened):
                self.register(assessment)

    def register(self, demand: Bill | MortgagePayment | PropertyTaxBill | Assessment) -> None:
        """The ledger's side of a demand: price it and make it this month's claim on the payer."""
        match demand:
            case Bill():
                claim = claims.Claim(
                    demand.cause_id,
                    demand.obligation_type,
                    demand.from_account,
                    demand.to_account,
                    self.market.amount(demand.amount, self.month),
                    demand.deduction,
                )
            case MortgagePayment():
                terms = demand.terms
                claim = claims.Claim(
                    f"{terms.liability_id}_payment_m{self.month}",
                    "mortgage_payment",
                    terms.borrower,
                    terms.lender,
                    checked_count(demand.interest + demand.principal, "money addition"),
                    demand,
                )
            case PropertyTaxBill() | Assessment():
                claim = claims.Claim(
                    demand.cause_id,
                    demand.obligation_type,
                    demand.from_account,
                    demand.to_account,
                    demand.amount,
                    demand.effect,
                )
        self.claims.entries.append(claim)

    def public_price(self, actor: str, asset: str, month: int) -> int:
        return self.holdings.public_price(actor, asset, self.market, month)

    def open_mail(self, actor: AgentId) -> list[Mail]:
        """What the world tells `actor` at open: each emitter's statement, its dues and last month's receipts."""
        self.validate_scope(actor)
        dues = self.claims.dues(actor)
        for due in dues:
            due._bind(self, self.rollout_id)
        return [
            self.market.statement(self.month),
            self.accounting.statement(actor, self.month),
            self.holdings.statement(actor, self.market, self.month),
            BondStatement(month=self.month, bonds=())
            if self.bonds is None
            else self.bonds.statement(actor, self.month),
            TlhStatement(month=self.month, portfolios=())
            if self.managed is None
            else self.managed.statement(actor, self.month),
            *dues,
            *self.previous_receipts,
        ]

    def apply(self, actor: str, action: Action, action_index: int) -> results.Executed | results.Rejected:
        self.validate_scope(actor)
        if not action.cause_id:
            return results.Rejected(reason=results.InvalidRequest(detail="empty cause ID"))
        if isinstance(action, PayClaim | Consume):
            receipt = payments.execute(self.accounting, self.month, self.claims, actor, action)
            target = None
            if isinstance(action, Consume):
                target = results.PaymentTarget(
                    to_account=action.to_account,
                    obligation_type="cash_spend",
                    label=action.component_id,
                    is_tax_payment=False,
                )
            elif action.claim.month == self.month and 0 <= action.claim.index < len(self.claims.entries):
                claim = self.claims.entries[action.claim.index]
                target = results.PaymentTarget(
                    to_account=claim.to_account,
                    obligation_type=claim.obligation_type,
                    label=claim.cause_id,
                    is_tax_payment=isinstance(claim.effect, claims.TaxPayment | claims.TaxTrueUp),
                )
            self.payments.append(
                results.Payment(
                    month=self.month,
                    action_index=action_index,
                    cause_id=action.cause_id,
                    from_account=action.from_account,
                    target=target,
                    receipt=receipt,
                )
            )
            if target is not None and (
                isinstance(receipt.outcome, results.Paid)
                or (isinstance(action, Consume) and isinstance(receipt.outcome.reason, results.InsufficientCash))
            ):
                self.obligations.append(
                    payments.ObligationOutcome(
                        self.month,
                        action.cause_id,
                        target.label,
                        target.obligation_type,
                        action.from_account,
                        target.to_account,
                        receipt.amount_requested,
                        receipt.amount_paid,
                        receipt.amount_requested - receipt.amount_paid,
                        isinstance(receipt.outcome, results.PaymentRejected),
                    )
                )
            if isinstance(receipt.outcome, results.PaymentRejected):
                return results.Rejected(reason=results.PaymentRejection(detail=receipt.outcome.reason))
            return results.Executed()
        # Financial request validators raise ValueError; arithmetic failures propagate and close the session.
        try:
            if isinstance(action, Transfer):
                self.accounting.transfer(self.month, action, actor=actor)
            elif isinstance(action, Buy | Sell):
                if action.agent_id != actor:
                    raise ValueError("wrong actor")
                price = self.public_price(actor, action.asset_id, self.month)
                if isinstance(action, Buy):
                    self.holdings.buy(self.accounting, self.month, action, price=price)
                else:
                    self.holdings.sell(self.accounting, self.month, action, price=price)
            else:
                raise ValueError("component request requires modeled financial effects")
        except ValueError as error:
            return results.Rejected(reason=results.InvalidRequest(detail=str(error)))
        return results.Executed()

    def unpaid_claims(self, actor: str) -> list[results.UnpaidClaim]:
        return [
            results.UnpaidClaim(
                id=id_,
                cause_id=claim.cause_id,
                obligation_type=claim.obligation_type,
                from_account=claim.from_account,
                to_account=claim.to_account,
                amount_due=claim.amount_due,
            )
            for id_, claim in self.claims.due(actor)
            if claim.amount_due > 0
        ]

    def settle_claims(self, product_actor: str | None = None) -> payments.Settlement:
        """The configured runner's grouped all-or-none settlement; `product_actor` scopes its shortfall."""
        settlement = payments.settle_grouped(self.accounting, self.claims, product_actor)
        self.obligations.extend(settlement.obligations)
        return settlement

    def close_books(self, *, failed: bool, mortgages: Sequence[Mortgage]) -> None:
        """Accrue, close the tax year on a successful December, and advance the month counter."""
        if self.agents:
            for claim in self.claims.entries:
                if not claim.paid:
                    if isinstance(claim.effect, claims.TaxPayment | claims.TaxTrueUp):
                        self.accounting.tax_payments.append(
                            TaxPaymentOutcome(
                                month=self.month,
                                cause_id=claim.cause_id,
                                agent_id=claim.from_account.agent_id,
                                obligation_type=claim.obligation_type,
                                amount_due=claim.amount_due,
                                amount_paid=0,
                                shortfall=claim.amount_due,
                            )
                        )
                    self.obligations.append(
                        payments.ObligationOutcome(
                            self.month,
                            claim.cause_id,
                            claim.cause_id,
                            claim.obligation_type,
                            claim.from_account,
                            claim.to_account,
                            claim.amount_due,
                            0,
                            claim.amount_due,
                            claim.amount_due > 0,
                        )
                    )
        if failed:
            self.failed_month = self.month
        else:
            if self.properties is not None:
                self.properties.accrue(self.accounting, self.month)
            if (self.month + 1) % 12 == 0:
                self.accounting.close_tax_year(self.month, mortgages)
                if self.properties is not None:
                    self.properties.reset_year()
        self.month += 1

    def marks(self) -> list[observations.TlhPortfolioObservation]:
        """Current managed-portfolio marks; none when no portfolio is declared."""
        return [] if self.managed is None else list(self.managed.marks.values())

    def book(self) -> Book:
        """The current books as a snapshot: state, not a stored history."""
        return Book(
            month=self.month,
            balances=[
                AccountBalance(account=account, balance=value)
                for account, value in sorted(
                    self.accounting.ledger.balances.items(), key=lambda item: (item[0].agent_id, item[0].account_id)
                )
            ],
            income=[
                IncomeState(agent_id=agent, income_source=income_source_wire_id(source), income=amount)
                for (agent, source), amount in self.accounting.tax.income.by_source.items()
            ],
            lots=[lot.snapshot() for lot in self.holdings.lots],
            bonds=None if self.bonds is None else self.bonds.snapshots(self.month, self.mark_month),
            properties=None if self.properties is None else self.properties.snapshots(),
            mortgages=self.mortgage_snapshots(),
            tax_liabilities=list(self.accounting.tax_liabilities),
            capital_gains=[
                CapitalGainState(
                    agent_id=agent, short_term_gain=year.short_term_gain, long_term_gain=year.long_term_gain
                )
                for agent, year in self.accounting.tax.years.items()
            ],
            tlh_portfolios=None
            if self.managed is None
            else [
                TlhPortfolioState(
                    portfolio_id=row.portfolio_id,
                    owner_agent_id=row.owner_agent_id,
                    account_id=row.account_id,
                    asset_id=row.asset_id,
                    value=row.value,
                    reported_tax_basis=row.reported_tax_basis,
                )
                for row in self.managed.marks.values()
            ],
            failed=self.failed_month is not None,
        )

    def holding_value(self, actor: str, month: int, account: str | None = None, asset: str | None = None) -> int:
        total = 0
        for lot in self.holdings.lots:
            spec = lot.spec
            if (
                spec.agent_id != actor
                or private_issuer(spec.asset_id) is not None
                or not lot.units_remaining
                or (account is not None and spec.account_id != account)
                or (asset is not None and spec.asset_id != asset)
            ):
                continue
            total = checked_count(
                total
                + position_value(
                    self.public_price(actor, spec.asset_id, month), lot.units_remaining, spec.quantity_scale
                ),
                "public value",
            )
        for row in self.marks():
            if (
                row.owner_agent_id == actor
                and (account is None or row.account_id == account)
                and (asset is None or row.asset_id == asset)
            ):
                total = checked_count(total + row.value, "public value")
        return total
