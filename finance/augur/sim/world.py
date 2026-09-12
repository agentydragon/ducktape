"""One rollout: financial books, tracked components, receipts and the monthly clock."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Literal

from finance.augur.sim import capture, claims, observations, payments, results
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
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.books import (
    AccountBalance,
    AccountRef,
    Book,
    CapitalGainState,
    IncomeState,
    MortgageState,
    TlhPortfolioState,
)
from finance.augur.sim.compiler.income_sources import income_source_wire_id
from finance.augur.sim.distributions import Distributions
from finance.augur.sim.held_bonds import HeldBonds
from finance.augur.sim.holdings import Holdings, private_issuer
from finance.augur.sim.managed import ComponentEffects, ManagedPortfolios
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, position_value
from finance.augur.sim.mortgage import Mortgage, MortgagePayment
from finance.augur.sim.prepared import CompiledRun, PreparedFixedAmount, PreparedTlhPortfolio
from finance.augur.sim.private_equity import PrivateEquity
from finance.augur.sim.property import Properties, mortgage_terms, principal
from finance.augur.sim.tlh import (
    ModeledRealizations,
    TlhMarketUpdate,
    TlhObservation,
    TlhOpening,
    TlhOpeningPosition,
    TlhPortfolio,
)
from finance.augur.sim.validation import validate

type Capture = Literal["summary", "dense", "forensic"]

_NO_REALIZATIONS = ModeledRealizations()


def _action_actor(action: Action) -> str:
    if isinstance(action, Transfer | PayClaim | Consume):
        return action.from_account.agent_id
    return action.agent_id


def validate_actor(run: CompiledRun, actor: str) -> None:
    """Reject prepared input whose configured strategies or unsupported domains overlap actor decisions."""
    scenario = run.scenario
    if scenario._target_allocation_policies or scenario._private_equity_tender_policies or scenario._scheduled_sales:
        raise ValueError("configured allocation, tender policies and scheduled sales overlap actor decisions")
    if (
        scenario._scheduled_property_purchases
        or scenario.scheduled_property_cashflows
        or scenario.recurring_property_cashflows
        or scenario._initial_primary_residences
        or scenario._primary_residence_events
        or scenario._property_rented_fraction_events
        or scenario._capital_improvement_events
        or scenario._property_sales
        or scenario._mortgage_interest_deduction_policies
        or scenario._property_tax_policies
        or scenario._federal_salt_deduction_policies
        or any(pool.asset_id.startswith("private_equity:") for pool in scenario.holding_pools)
    ):
        raise ValueError(
            "the scoped actor control supports public securities, cash and due claims, not housing or private equity"
        )
    if any(
        claim.from_account.agent_id != actor for claim in (*scenario.obligations, *scenario.recurring_obligations)
    ) or any(profile.agent_id != actor for profile in scenario.tax_profiles):
        raise ValueError(
            "only the decision-making household may have payment claims; counterparties use scheduled cashflows"
        )


class World:
    """Track an agent, `start`, then `step` until `finished`; the batch session drives several worlds the same way."""

    def __init__(
        self,
        run: CompiledRun,
        rollout_id: int,
        *,
        capture_mode: Capture,
        actor: str | None,
        product_actor: str | None = None,
    ) -> None:
        if not isinstance(run, CompiledRun):
            raise TypeError("execution requires a CompiledRun, not serialized input")
        if capture_mode not in ("summary", "dense", "forensic"):
            raise ValueError("capture must be summary, dense or forensic")
        self.run = run
        self.scenario = run.scenario
        self.market = MarketPath(run, rollout_id)
        self.rollout_id = rollout_id
        self.capture_mode = capture_mode
        self.actor = actor
        self.product_actor = product_actor
        self.agents: list[EconomicAgent] = []
        self.specs = {spec.portfolio_id: spec for spec in self.scenario.tlh_portfolios}
        self.portfolios = {
            spec.portfolio_id: TlhPortfolio(
                spec.assumptions,
                TlhOpening(
                    month=-1,
                    price=self.market.value(f"security:{spec.asset_id}", 0),
                    quantity_scale=spec.quantity_scale,
                    positions=tuple(
                        TlhOpeningPosition(lot.units, lot.basis, lot.purchase_month) for lot in spec.initial_cohorts
                    ),
                ),
            )
            for spec in self.specs.values()
        }
        opening = [self.statement(spec, self.portfolios[spec.portfolio_id].observe()) for spec in self.specs.values()]
        self.accounting = Accounting(
            run.scenario.accounts, run.scenario.tax_profiles, run.scenario.income_sources, capture=capture_mode
        )
        self.holdings = Holdings(run.scenario, self.accounting)
        self.managed = ManagedPortfolios(run.scenario, self.accounting, opening)
        self.properties = Properties(run.scenario, self.accounting)
        self.bonds = HeldBonds(run.scenario.initial_bonds, self.market)
        self.distributions = Distributions()
        self.private_equity = PrivateEquity()
        self.claims = claims.Claims(0, [])
        self.month = 0
        self.failed_month: int | None = None
        self.obligations: list[payments.ObligationOutcome] = []
        self.payments: list[results.Payment] = []
        self.books: list[Book] = []
        self.cash_series: list[results.CashSeries] = [
            results.CashSeries(account=account.account, values=[])
            for account in self.scenario.accounts
            if account.account.agent_id == actor
        ]
        self.holding_series: dict[tuple[AccountRef, str], list[int]] = {}
        self.bond_series = [
            results.BondSeries(
                account=AccountRef(agent_id=bond.agent_id, account_id=bond.account_id), bond_id=bond.bond_id, values=[]
            )
            for bond in self.bonds.terms
            if bond.agent_id == actor
        ]
        self.product_metrics: list[tuple[int, int, int, int, int, int, int]] = []
        self.mortgages: dict[str, Mortgage] = {}
        self.mortgage_payments: dict[str, MortgagePayment] = {}
        self.previous_receipts: list[results.Receipt] = []
        self.receipts: list[results.Receipt] = []
        self.stop: results.Stop | None = None
        self.failed = False
        self.shortfall = 0
        self.result: capture.WorldResult | None = None
        self.started = False
        for scoped in (actor, product_actor):
            if scoped is not None:
                self.validate_scope(scoped)
        self.snapshot([], 0)

    @staticmethod
    def statement(spec: PreparedTlhPortfolio, value: TlhObservation) -> observations.TlhPortfolioObservation:
        return observations.TlhPortfolioObservation(
            portfolio_id=spec.portfolio_id,
            owner_agent_id=spec.owner_agent_id,
            account_id=spec.account_id,
            asset_id=spec.asset_id,
            value=value.value,
            reported_tax_basis=value.reported_tax_basis,
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
            observation=self.statement(spec, candidate.observe()),
            cash_account_id=cash_account_id,
            cash_amount=cash_amount,
            short_term_gain=realizations.short_term_gain,
            long_term_gain=realizations.long_term_gain,
        )

    def track(self, agent: EconomicAgent) -> None:
        """Register the decision-making agent; consistency with the prepared input is checked here."""
        if self.started:
            raise ValueError("track components before starting the world")
        if not isinstance(agent, EconomicAgent):
            raise TypeError("only EconomicAgent subclasses can be tracked")
        if self.agents:
            raise ValueError("one decision-making agent per world until multi-actor sequencing is decided")
        if agent.agent_id != self.actor:
            raise ValueError(f"tracked agent {agent.agent_id!r} is not this world's actor {self.actor!r}")
        validate_actor(self.run, agent.agent_id)
        validate(self.run)
        self.agents.append(agent)

    @property
    def finished(self) -> bool:
        return self.result is not None

    def start(self) -> None:
        if self.started:
            raise ValueError("invalid world lifecycle state: already started")
        self.started = True
        self.open_month()

    def step(self) -> None:
        """One month: each tracked agent decides once on its opened view, actions execute in order, the month closes."""
        if not self.started or self.result is not None:
            raise ValueError("world is not running")
        if not self.agents:
            raise ValueError("step needs a tracked agent; scripted-only paths run through the batch session")
        for agent in self.agents:
            actions = agent.decide(self.observe(agent.agent_id))
            self.begin_actions(actions)
            for action in actions:
                if isinstance(self.execute(action).outcome, results.Rejected):
                    break
        self.close_month()

    def rollout(self) -> results.Rollout:
        if self.result is None:
            raise ValueError("world has not finished")
        return self.result.rollout(self.receipts, self.previous_receipts, self.stop)

    def open_month(self) -> None:
        """Components advance before any investor operation, then contracts raise this month's claims."""
        for spec in self.specs.values():
            current = self.portfolios[spec.portfolio_id]
            for index, distribution in enumerate(self.scenario.distributions):
                if (distribution.agent_id, distribution.holding_account_id, distribution.asset_id) != (
                    spec.owner_agent_id,
                    spec.account_id,
                    spec.asset_id,
                ):
                    continue
                rate = self.market.value(f"security_distribution:{spec.asset_id}", self.month)
                self.managed.distribute(self.scenario, self.accounting, self.month, index, current._distribution(rate))
            candidate = deepcopy(current)
            realized = candidate.advance(
                TlhMarketUpdate(self.month, self.market.value(f"security:{spec.asset_id}", self.month))
            )
            self.managed.settle(
                self.scenario,
                self.accounting,
                self.month,
                spec.owner_agent_id,
                f"tlh:{spec.portfolio_id}:advance:m{self.month}",
                self.effects(spec, candidate, None, 0, realized),
                operation="modeled_realization",
            )
            self.portfolios[spec.portfolio_id] = candidate
        self.open_mortgages()

    def open_mortgages(self) -> None:
        """Originate/pay off configured contracts, then quote this month's installments."""
        candidates = {}
        for purchase in self.scenario._scheduled_property_purchases:
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
            payment = loan.payment(
                self.month, self.mortgage_principal(id_), self.property_rented_fraction(loan.terms.property_id)
            )
            if payment is not None:
                self.mortgage_payments[id_] = payment
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

    def execute(self, action: Action) -> results.Receipt:
        if self.failed:
            raise ValueError("cannot act on a stopped rollout")
        index = len(self.previous_receipts)
        actor = self.actor if self.actor is not None else _action_actor(action)
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
        if self.capture_mode != "summary":
            self.receipts.append(receipt)
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
        candidate = deepcopy(current)
        if isinstance(action, Contribute):
            contribution = candidate.contribute(action.amount)
            effects = self.effects(spec, candidate, action.cash_account_id, -contribution.cash_paid)
        else:
            withdrawal = candidate.withdraw(action.amount) if isinstance(action, Withdraw) else candidate.liquidate()
            effects = self.effects(
                spec, candidate, action.cash_account_id, withdrawal.cash_received, withdrawal.realizations
            )
        self.managed.validate_request(action, effects)
        self.managed.settle(
            self.scenario,
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
        """Stop on unpaid claims, mark components, close the books, then open the next month unless finished."""
        if self.actor is not None:
            unpaid = self.unpaid_claims(self.actor)
            if unpaid and self.stop is None:
                self.failed = True
                self.stop = results.UnpaidClaims(month=self.month, claims=[claim.id for claim in unpaid])
        marks = [
            self.statement(
                spec,
                self.portfolios[spec.portfolio_id]._observe_at_price(
                    self.market.value(f"security:{spec.asset_id}", self.month + (not self.failed))
                ),
            )
            for spec in self.specs.values()
        ]
        self.managed.mark(self.scenario, marks)
        for id_ in (
            claim.effect.terms.liability_id
            for claim in self.claims.entries
            if claim.paid and isinstance(claim.effect, MortgagePayment)
        ):
            self.mortgages[id_].record_payment(self.mortgage_payments[id_], self.mortgage_principal(id_))
        reset_year = not self.failed and (self.month + 1) % 12 == 0
        snapshots = self.mortgage_snapshots()
        if reset_year:
            snapshots = [
                snapshot.model_copy(update={"interest_paid_ytd": 0, "rental_interest_paid_ytd": 0})
                for snapshot in snapshots
            ]
        self.close_books(
            failed=self.failed, shortfall=self.shortfall, mortgages=list(self.mortgages.values()), snapshots=snapshots
        )
        if reset_year:
            for loan in self.mortgages.values():
                loan.reset_year()
        if self.failed or self.month == self.scenario.horizon_months:
            self.result = self.finish(snapshots)
        else:
            self.open_month()

    def validate_scope(self, actor: str) -> None:
        if not any(account.agent_id == actor for account in self.accounting.declared):
            raise ValueError(f"unknown actor {actor!r}")
        for pool in self.scenario.holding_pools:
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
        purchase = next(
            (
                purchase
                for purchase in self.scenario._scheduled_property_purchases
                if purchase.mortgage is not None and purchase.mortgage.liability_id == liability_id
            ),
            None,
        )
        if purchase is None:
            raise ValueError("unknown mortgage liability")
        return principal(self.accounting, purchase)

    def property_rented_fraction(self, property_id: str) -> int:
        return self.properties.properties[property_id].state.rented_fraction_ppb

    def prepare_month(
        self, month: int, originations: Mapping[str, Mortgage], mortgages: Mapping[str, Mortgage]
    ) -> tuple[list[str], list[str]]:
        if month != self.month or month >= self.scenario.horizon_months:
            raise ValueError("invalid financial month")
        self.claims = claims.Claims(month, [])
        self.properties.assign_residences(self.scenario, month)
        paid_off = self.properties.lifecycle(self.scenario, self.accounting, self.market, month, mortgages)
        self.bonds.advance(self.accounting, month)
        self.distributions.advance(self.scenario, self.accounting, self.holdings, self.market, month)
        originated = self.properties.purchase(self.scenario, self.accounting, month, originations)
        active = {row.property_id for row in self.properties.snapshots() if row.active}
        flows = [flow for flow in self.scenario.scheduled_transfers if flow.month == month]
        recurring = [
            flow
            for flow in self.scenario.recurring_transfers
            if flow.start_month <= month and (flow.end_month is None or month <= flow.end_month)
        ]
        property_flows = [
            flow
            for flow in self.scenario.scheduled_property_cashflows
            if flow.month == month and flow.property_id in active
        ]
        property_recurring = [
            flow
            for flow in self.scenario.recurring_property_cashflows
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
        seen = set()
        for installment in installments:
            id_ = installment.terms.liability_id
            if (
                id_ in seen
                or installment.terms.origination_month >= self.month
                or not 0 <= installment.principal <= self.mortgage_principal(id_)
                or self.mortgage_principal(id_) <= 0
                or not 0 <= installment.rental_interest <= installment.interest
            ):
                raise ValueError("invalid mortgage installment")
            seen.add(id_)
        self.claims = claims.assemble(
            self.scenario,
            self.market,
            self.month,
            self.properties.snapshots(),
            installments,
            self.accounting.tax_liabilities,
        )

    def public_price(self, actor: str, asset: str, month: int) -> int:
        if private_issuer(asset) is not None or not any(
            pool.agent_id == actor and pool.asset_id == asset for pool in self.scenario.holding_pools
        ):
            raise ValueError("asset has no declared public holding pool")
        return self.market.value(f"security:{asset}", month)

    def observe(self, actor: str) -> observations.Observation:
        self.validate_scope(actor)
        positions = []
        for lot in self.holdings.lots:
            spec = lot.spec
            if spec.agent_id != actor or lot.units_remaining == 0 or private_issuer(spec.asset_id) is not None:
                continue
            price = self.public_price(actor, spec.asset_id, self.month)
            positions.append(
                observations.PublicPosition(
                    account_id=spec.account_id,
                    asset_id=spec.asset_id,
                    lot_id=spec.lot_id,
                    purchase_month=spec.purchase_month,
                    units=lot.units_remaining,
                    quantity_scale=spec.quantity_scale,
                    book_basis=lot.basis_remaining,
                    price=price,
                    value=position_value(price, lot.units_remaining, spec.quantity_scale),
                )
            )
        accounts = tuple(
            (account.account.account_id, self.accounting.ledger.balance(account.account))
            for account in self.scenario.accounts
            if account.account.agent_id == actor
        )
        bonds = []
        for bond in self.bonds.terms:
            if bond.agent_id != actor:
                continue
            carrying = self.bonds.held_principal(bond, self.month + 1, self.month)
            if carrying is None:
                continue
            coupon = (
                observations.FixedCoupon(amount=bond.coupon.amount)
                if isinstance(bond.coupon, PreparedFixedAmount)
                else observations.IndexedCoupon(annual_rate_ppb=bond.coupon.annual_rate_ppb)
            )
            bonds.append(
                observations.HeldBond(
                    bond_id=bond.bond_id,
                    account_id=bond.account_id,
                    issuer_jurisdiction_id=bond.issuer_jurisdiction_id,
                    face_value=bond.face_value,
                    purchase_price=bond.purchase_price,
                    coupon=coupon,
                    coupon_period_months=bond.coupon_period_months,
                    purchase_month=bond.purchase_month_index,
                    maturity_month=bond.maturity_month_index,
                    principal=carrying,
                )
            )
        observation = observations.Observation(
            agent_id=actor,
            month=self.month,
            cpi=(self.market.value("inflation", self.month), self.market.value("inflation", 0))
            if "inflation" in self.market.series
            else None,
            cash=checked_count(sum(amount for _, amount in accounts), "actor cash"),
            public_holdings=checked_count(sum(position.value for position in positions), "public value"),
            accounts=accounts,
            holding_pools=tuple(
                observations.HoldingPool(
                    account_id=pool.account_id,
                    asset_id=pool.asset_id,
                    quantity_scale=pool.quantity_scale,
                    price=self.public_price(actor, pool.asset_id, self.month),
                )
                for pool in self.scenario.holding_pools
                if pool.agent_id == actor and private_issuer(pool.asset_id) is None
            ),
            public_positions=tuple(positions),
            held_bonds=tuple(bonds),
            tlh_portfolios=tuple(row for row in self.managed.marks.values() if row.owner_agent_id == actor),
            claims=tuple(
                observations.Claim(
                    month=id_.month,
                    index=id_.index,
                    cause_id=claim.cause_id,
                    obligation_type=claim.obligation_type,
                    from_account=claim.from_account,
                    to_account=claim.to_account,
                    amount_due=claim.amount_due,
                )
                for id_, claim in self.claims.due(actor)
            ),
            previous_receipts=tuple(self.previous_receipts),
        )
        for claim in observation.claims:
            claim._bind(self, self.rollout_id)
        return observation

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
            if self.actor is not None:
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
                    self.holdings.buy(self.scenario, self.accounting, self.month, action, price=price)
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

    def settle_claims(self) -> payments.Settlement:
        settlement = payments.settle_grouped(self.accounting, self.claims, self.product_actor)
        self.obligations.extend(settlement.obligations)
        return settlement

    def close_books(
        self, *, failed: bool, shortfall: int, mortgages: Sequence[Mortgage], snapshots: list[MortgageState]
    ) -> None:
        if self.actor is not None:
            for claim in self.claims.entries:
                if not claim.paid:
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
            self.properties.accrue(self.accounting, self.month)
            if (self.month + 1) % 12 == 0:
                self.accounting.close_tax_year(self.scenario, self.month, mortgages)
                self.properties.reset_year()
        self.month += 1
        self.snapshot(snapshots, shortfall)

    def book(self, mortgages: list[MortgageState]) -> Book:
        mark = self.month if self.failed_month is None else self.failed_month
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
            bonds=self.bonds.snapshots(self.month, mark),
            properties=self.properties.snapshots(),
            mortgages=mortgages,
            tax_liabilities=list(self.accounting.tax_liabilities),
            capital_gains=[
                CapitalGainState(
                    agent_id=agent, short_term_gain=year.short_term_gain, long_term_gain=year.long_term_gain
                )
                for agent, year in self.accounting.tax.years.items()
            ],
            tlh_portfolios=[
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
        for row in self.managed.marks.values():
            if (
                row.owner_agent_id == actor
                and (account is None or row.account_id == account)
                and (asset is None or row.asset_id == asset)
            ):
                total = checked_count(total + row.value, "public value")
        return total

    def snapshot(self, mortgages: list[MortgageState], shortfall: int) -> None:
        mark = self.month if self.failed_month is None else self.failed_month
        if self.accounting.capture != "summary":
            self.books.append(self.book(mortgages))
        if self.actor is not None:
            for series in self.cash_series:
                series.values.append(self.accounting.ledger.balance(series.account))
            for bond, series in zip(
                (bond for bond in self.bonds.terms if bond.agent_id == self.actor), self.bond_series, strict=True
            ):
                value = self.bonds.held_principal(bond, self.month, mark)
                series.values.append(0 if value is None else value)
            keys = {
                (AccountRef(agent_id=lot.spec.agent_id, account_id=lot.spec.account_id), lot.spec.asset_id)
                for lot in self.holdings.lots
                if lot.spec.agent_id == self.actor
            }
            keys.update(
                (AccountRef(agent_id=row.owner_agent_id, account_id=row.account_id), row.asset_id)
                for row in self.managed.marks.values()
                if row.owner_agent_id == self.actor
            )
            for key in keys:
                self.holding_series.setdefault(key, [0] * self.month)
            for (account, asset), values in self.holding_series.items():
                values.append(self.holding_value(self.actor, mark, account.account_id, asset))
        actor = self.product_actor
        if actor is not None:
            cash = sum(
                self.accounting.ledger.balance(account)
                for account in self.accounting.declared
                if account.agent_id == actor
            )
            private = sum(
                position_value(
                    self.market.value(f"private_equity_mark:{issuer}", mark),
                    lot.units_remaining,
                    lot.spec.quantity_scale,
                )
                for lot in self.holdings.lots
                if lot.spec.agent_id == actor
                and lot.units_remaining
                and (issuer := private_issuer(lot.spec.asset_id)) is not None
            )
            property_value = sum(
                self.properties.market_value(purchase, self.market, mark)
                for purchase in self.scenario._scheduled_property_purchases
                if purchase.buyer_agent_id == actor
                and purchase.property_id in self.properties.properties
                and self.properties.properties[purchase.property_id].state.active
                and f"home_value:{purchase.location_id}" in self.market.series
            )
            debt = sum(loan.principal for loan in mortgages if loan.agent_id == actor)
            bonds = sum(row.principal for row in self.bonds.snapshots(self.month, mark) if row.agent_id == actor)
            self.product_metrics.append(
                (
                    checked_count(cash, "product cash"),
                    self.holding_value(actor, mark),
                    checked_count(private, "product private equity"),
                    checked_count(property_value, "product property"),
                    checked_count(debt, "product mortgage"),
                    shortfall,
                    checked_count(bonds, "product bonds"),
                )
            )

    def finish(self, mortgages: list[MortgageState]) -> capture.WorldResult:
        if self.failed_month is None and self.month != self.scenario.horizon_months:
            raise ValueError("only terminal rollouts can be finalized")
        book = self.book(mortgages)
        summary = None
        if self.actor is not None:
            summary = results.Summary(
                actor_id=self.actor,
                cash=self.cash_series,
                public_holdings=[
                    results.HoldingSeries(account=account, asset_id=asset, values=values)
                    for (account, asset), values in sorted(
                        self.holding_series.items(),
                        key=lambda item: (item[0][0].agent_id, item[0][0].account_id, item[0][1]),
                    )
                ],
                bond_principal=self.bond_series,
                payments=self.payments,
                unpaid_claims=self.unpaid_claims(self.actor),
                tax_accruals=self.accounting.tax_accruals,
                tax_payments=self.accounting.tax_payments,
                tax_settlements=self.accounting.tax_settlements,
                ending_book=book,
                ending_mark_month=self.month if self.failed_month is None else self.failed_month,
                last_receipts=[],
            )
        financial = None
        configured = None
        if self.accounting.capture != "summary":
            financial = capture.FinancialOutput(
                rollout_id=self.market.rollout_id,
                months=self.books,
                journal=self.accounting.journal,
                transfers=self.accounting.transfers,
                dispositions=self.holdings.dispositions,
                tlh_financial_effects=self.managed.effects,
                private_equity_events=self.private_equity.events,
                private_equity_opportunities=self.private_equity.opportunities,
                obligations=self.obligations,
                tax_accruals=self.accounting.tax_accruals,
                tax_payments=self.accounting.tax_payments,
                tax_settlements=self.accounting.tax_settlements,
                bond_cashflows=self.bonds.cashflows,
                distributions=self.distributions.outcomes + self.managed.distributions,
                property_purchases=self.properties.purchases,
                primary_residence_events=self.properties.residences,
                property_rented_fraction_events=self.properties.rented_fractions,
                capital_improvements=self.properties.improvements,
                property_sales=self.properties.sales,
                mortgage_originations=self.properties.originations,
                mortgage_payments=self.accounting.mortgage_payments,
                failed_month=self.failed_month,
            )
        elif summary is None:
            configured = capture.ConfiguredSummary(
                rollout_id=self.market.rollout_id,
                ending_balances=book.balances,
                ending_bonds=book.bonds,
                ending_properties=book.properties,
                ending_mortgages=book.mortgages,
                ending_tax_liabilities=book.tax_liabilities,
                ending_tlh_portfolios=list(self.managed.marks.values()),
                journal_entry_count=self.accounting.journal_entry_count,
                disposition_count=self.holdings.disposition_count,
                private_equity_event_count=len(self.private_equity.events),
                private_equity_opportunity_count=len(self.private_equity.opportunities),
                tax_accrual_count=len(self.accounting.tax_accruals),
                tax_payment_count=len(self.accounting.tax_payments),
                tax_settlement_count=len(self.accounting.tax_settlements),
                bond_cashflow_count=self.bonds.cashflow_count,
                distribution_count=self.distributions.count + self.managed.distribution_count,
                property_purchase_count=len(self.properties.purchases),
                primary_residence_event_count=len(self.properties.residences),
                property_rented_fraction_event_count=len(self.properties.rented_fractions),
                capital_improvement_count=len(self.properties.improvements),
                property_sale_count=len(self.properties.sales),
                mortgage_payment_count=len(self.accounting.mortgage_payments),
                failed_month=self.failed_month,
            )
        return capture.WorldResult(
            self.market.rollout_id,
            summary,
            financial,
            capture.event_log(financial) if financial is not None else None,
            configured,
            self.product_metrics,
        )
