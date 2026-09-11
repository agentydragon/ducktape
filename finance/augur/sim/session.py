"""Python-owned monthly orchestration with one ordered batch policy response per month."""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

from finance.augur.sim import results
from finance.augur.sim.actions import (
    Action,
    ClaimId,
    Consume,
    Contribute,
    DecisionActions,
    Liquidate,
    PayClaim,
    Transfer,
    Withdraw,
)
from finance.augur.sim.books import MortgageState
from finance.augur.sim.capture import WorldResult
from finance.augur.sim.managed import ComponentEffects
from finance.augur.sim.mortgage import Mortgage, MortgagePayment
from finance.augur.sim.observations import Decision, Observation, TlhPortfolioObservation
from finance.augur.sim.prepared import CompiledRun, PreparedTlhPortfolio
from finance.augur.sim.property import mortgage_terms
from finance.augur.sim.tlh import (
    ModeledRealizations,
    TlhMarketUpdate,
    TlhObservation,
    TlhOpening,
    TlhOpeningPosition,
    TlhPortfolio,
)
from finance.augur.sim.validation import validate
from finance.augur.sim.world import World

type Capture = Literal["summary", "dense", "forensic"]

_NO_REALIZATIONS = ModeledRealizations()


@dataclass
class _Path:
    world: World
    portfolios: dict[str, TlhPortfolio]
    mortgages: dict[str, Mortgage] = field(default_factory=dict)
    mortgage_payments: dict[str, MortgagePayment] = field(default_factory=dict)
    previous_receipts: list[results.Receipt] = field(default_factory=list)
    receipts: list[results.Receipt] = field(default_factory=list)
    stop: results.Stop | None = None
    failed: bool = False
    shortfall: int = 0
    result: WorldResult | None = None


def _action_actor(action: Action) -> str:
    if isinstance(action, Transfer | PayClaim | Consume):
        return action.from_account.agent_id
    return action.agent_id


def _validate_actor(run: CompiledRun, actor: str) -> None:
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


class _Session:
    """Own selected paths, time, components and receipts; worlds own only financial books."""

    def __init__(
        self,
        run: CompiledRun,
        actor: str | None,
        rollout_ids: list[int],
        *,
        capture: Capture,
        configured: bool = False,
        product_actor: str | None = None,
    ) -> None:
        if not isinstance(run, CompiledRun):
            raise TypeError("execution requires a CompiledRun, not serialized input")
        if (
            not rollout_ids
            or len(set(rollout_ids)) != len(rollout_ids)
            or any(
                not isinstance(id_, int) or isinstance(id_, bool) or not 0 <= id_ < run.rollout_count
                for id_ in rollout_ids
            )
        ):
            raise ValueError("selected rollout IDs must be unique, nonempty and in range")
        if capture not in ("summary", "dense", "forensic"):
            raise ValueError("capture must be summary, dense or forensic")
        if not configured:
            if actor is None:
                raise ValueError("action sessions require an actor")
            _validate_actor(run, actor)
        validate(run)
        self.run = run
        self.actor = actor
        self.configured = configured
        self.capture = capture
        self.month = 0
        self.started = False
        self.closed = False
        self.specs = {spec.portfolio_id: spec for spec in run.scenario.tlh_portfolios}
        self.series = {series.series_id: series for series in run.series}
        self.paths: dict[int, _Path] = {}
        for rollout_id in rollout_ids:
            portfolios = {
                spec.portfolio_id: TlhPortfolio(
                    spec.assumptions,
                    TlhOpening(
                        month=-1,
                        price=self.price(f"security:{spec.asset_id}", rollout_id, 0),
                        quantity_scale=spec.quantity_scale,
                        positions=tuple(
                            TlhOpeningPosition(lot.units, lot.basis, lot.purchase_month) for lot in spec.initial_cohorts
                        ),
                    ),
                )
                for spec in self.specs.values()
            }
            opening = [self.statement(spec, portfolios[spec.portfolio_id].observe()) for spec in self.specs.values()]
            world = World(
                run,
                rollout_id,
                opening,
                capture_mode=capture,
                actor=None if configured else actor,
                product_actor=product_actor,
            )
            self.paths[rollout_id] = _Path(world, portfolios)

    def price(self, series_id: str, rollout_id: int, month: int) -> int:
        series = self.series[series_id]
        return series.values[rollout_id * series.snapshots + month]

    @staticmethod
    def statement(spec: PreparedTlhPortfolio, value: TlhObservation) -> TlhPortfolioObservation:
        return TlhPortfolioObservation(
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

    def active(self) -> dict[int, _Path]:
        return {id_: path for id_, path in self.paths.items() if path.result is None}

    def is_finished(self) -> bool:
        return self.started and all(path.result is not None for path in self.paths.values())

    def _check_open(self) -> None:
        if self.closed or self.is_finished():
            raise ValueError("session is finished, aborted or closed")

    def start(self) -> None:
        self._check_open()
        if self.started:
            raise ValueError("invalid session lifecycle state: already started")
        self.started = True
        self.opening()

    def opening(self) -> None:
        """The manager advances before any investor operation, including configured sales."""
        for rollout_id, path in self.active().items():
            self.open_mortgages(path)
            for spec in self.specs.values():
                current = path.portfolios[spec.portfolio_id]
                for index, distribution in enumerate(self.run.scenario.distributions):
                    if (distribution.agent_id, distribution.holding_account_id, distribution.asset_id) != (
                        spec.owner_agent_id,
                        spec.account_id,
                        spec.asset_id,
                    ):
                        continue
                    rate = self.price(f"security_distribution:{spec.asset_id}", rollout_id, self.month)
                    path.world.managed.distribute(
                        self.run.scenario, path.world.accounting, self.month, index, current._distribution(rate)
                    )
                candidate = deepcopy(current)
                realized = candidate.advance(
                    TlhMarketUpdate(self.month, self.price(f"security:{spec.asset_id}", rollout_id, self.month))
                )
                path.world.managed.settle(
                    self.run.scenario,
                    path.world.accounting,
                    self.month,
                    spec.owner_agent_id,
                    f"tlh:{spec.portfolio_id}:advance:m{self.month}",
                    self.effects(spec, candidate, None, 0, realized),
                    operation="modeled_realization",
                )
                path.portfolios[spec.portfolio_id] = candidate

    def open_mortgages(self, path: _Path) -> None:
        """Originate/pay off configured contracts, then quote this month's installments."""
        candidates = {}
        for purchase in self.run.scenario._scheduled_property_purchases:
            financing = purchase.mortgage
            if purchase.month != self.month or financing is None:
                continue
            candidates[financing.liability_id] = Mortgage(mortgage_terms(purchase))
        originated, paid_off = path.world.prepare_month(self.month, candidates, path.mortgages)
        for id_ in paid_off:
            path.mortgages[id_].payoff()
        for id_ in originated:
            path.mortgages[id_] = candidates[id_]
        path.mortgage_payments = {}
        for id_, loan in path.mortgages.items():
            if not loan.active:
                continue
            payment = loan.payment(
                self.month,
                path.world.mortgage_principal(id_),
                path.world.property_rented_fraction(loan.terms.property_id),
            )
            if payment is not None:
                path.mortgage_payments[id_] = payment
        path.world.assemble_claims(list(path.mortgage_payments.values()))

    @staticmethod
    def mortgage_snapshots(path: _Path) -> list[MortgageState]:
        return [loan.observe(path.world.mortgage_principal(id_)) for id_, loan in path.mortgages.items()]

    def observe(self, rollout_id: int, actor: str) -> Observation:
        path = self.paths[rollout_id]
        observation = path.world.observe(actor)
        for claim in observation.claims:
            claim._bind(self, rollout_id)
        return observation.model_copy(update={"previous_receipts": tuple(path.previous_receipts)})

    def begin_actions(self, responses: list[DecisionActions]) -> None:
        """Validate the complete routing envelope before executing any action."""
        self._check_open()
        if not self.started:
            raise ValueError("invalid session lifecycle state: not started")
        if not all(isinstance(response, DecisionActions) for response in responses):
            raise TypeError("responses must be DecisionActions")
        if any(
            not isinstance(key, int) or isinstance(key, bool)
            for response in responses
            for key in (response.rollout_id, response.month)
        ):
            raise TypeError("response rollout ID and month must be integers")
        keys = [(response.rollout_id, response.month) for response in responses]
        if len(set(keys)) != len(keys) or set(keys) != {(id_, self.month) for id_ in self.active()}:
            raise ValueError("responses must name each active path/month exactly once")
        for response in responses:
            for action in response.actions:
                if isinstance(action, PayClaim) and not action.claim._belongs_to(self, response.rollout_id):
                    raise ValueError("claim belongs to a different rollout or session")
        for path in self.active().values():
            path.previous_receipts = []

    def apply(self, rollout_id: int, action: Action) -> results.Receipt:
        path = self.paths[rollout_id]
        if path.failed:
            raise ValueError("cannot act on a stopped rollout")
        index = len(path.previous_receipts)
        actor = _action_actor(action) if self.configured else self.actor
        if actor is None:
            raise RuntimeError("investor actions require an actor")
        if isinstance(action, Contribute | Withdraw | Liquidate):
            outcome = self._component_action(path, actor, action)
        else:
            outcome = path.world.apply(actor, action, index)
        historical_action = action
        if isinstance(action, PayClaim):
            historical_action = action.model_copy(
                update={"claim": ClaimId(month=action.claim.month, index=action.claim.index)}
            )
        receipt = results.Receipt(month=self.month, action_index=index, action=historical_action, outcome=outcome)
        path.previous_receipts.append(receipt)
        if self.capture != "summary":
            path.receipts.append(receipt)
        if isinstance(outcome, results.Rejected):
            path.failed = True
            path.stop = results.RejectedAction(month=self.month, action_index=index)
        return receipt

    def _component_action(
        self, path: _Path, actor: str, action: Contribute | Withdraw | Liquidate
    ) -> results.Executed | results.Rejected:
        def reject(detail: str) -> results.Rejected:
            return results.Rejected(reason=results.InvalidRequest(detail=detail))

        spec = self.specs.get(action.portfolio_id)
        if spec is None or action.agent_id != spec.owner_agent_id or actor != action.agent_id:
            return reject("unknown or unowned TLH portfolio")
        if not action.cause_id:
            return reject("TLH cause identifier must not be empty")
        available = path.world.account_balance(actor, action.cash_account_id)
        if available is None:
            return reject("unknown TLH cash account")
        current = path.portfolios[spec.portfolio_id]
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
        path.world.managed.validate_request(action, effects)
        path.world.managed.settle(
            self.run.scenario,
            path.world.accounting,
            self.month,
            actor,
            action.cause_id,
            effects,
            operation="contribution" if isinstance(action, Contribute) else "redemption",
        )
        path.portfolios[spec.portfolio_id] = candidate
        return results.Executed()

    def close_month(self) -> None:
        for rollout_id, path in self.active().items():
            if not self.configured:
                if self.actor is None:
                    raise RuntimeError("action sessions require an actor")
                unpaid = path.world.unpaid_claims(self.actor)
                if unpaid and path.stop is None:
                    path.failed = True
                    path.stop = results.UnpaidClaims(month=self.month, claims=[claim.id for claim in unpaid])
            marks = [
                self.statement(
                    spec,
                    path.portfolios[spec.portfolio_id]._observe_at_price(
                        self.price(f"security:{spec.asset_id}", rollout_id, self.month + (not path.failed))
                    ),
                )
                for spec in self.specs.values()
            ]
            path.world.managed.mark(self.run.scenario, marks)
            for id_ in (
                claim.effect.terms.liability_id
                for claim in path.world.claims.entries
                if claim.paid and isinstance(claim.effect, MortgagePayment)
            ):
                path.mortgages[id_].record_payment(path.mortgage_payments[id_], path.world.mortgage_principal(id_))
            reset_year = not path.failed and (self.month + 1) % 12 == 0
            snapshots = self.mortgage_snapshots(path)
            if reset_year:
                snapshots = [
                    snapshot.model_copy(update={"interest_paid_ytd": 0, "rental_interest_paid_ytd": 0})
                    for snapshot in snapshots
                ]
            path.world.close_month(
                failed=path.failed,
                shortfall=path.shortfall,
                mortgages=list(path.mortgages.values()),
                snapshots=snapshots,
            )
            if reset_year:
                for loan in path.mortgages.values():
                    loan.reset_year()
            if path.failed or self.month + 1 == self.run.scenario.horizon_months:
                path.result = path.world.finish(snapshots)
        self.month += 1
        if not self.is_finished():
            self.opening()

    def close(self) -> None:
        self.closed = True
        self.paths.clear()


class ActionSession:
    """One household's batch session; only the caller invokes policy code.

    Submit one keyed ordered response per current path. Rejection stops that path,
    preserving earlier effects; no retries or engine-selected rescue actions occur.
    """

    def __init__(self, run: CompiledRun, actor: str, rollout_ids: list[int], *, capture: Capture = "forensic") -> None:
        self._session = _Session(run, actor, rollout_ids, capture=capture)

    def _result(self) -> list[Decision] | results.Finished:
        session = self._session
        if session.is_finished():
            rollouts = []
            for path in session.paths.values():
                if path.result is None:
                    raise RuntimeError("finished session has an unfinished rollout")
                rollouts.append(path.result.rollout(path.receipts, path.previous_receipts, path.stop))
            return results.Finished(rollouts=rollouts)
        if session.actor is None:
            raise RuntimeError("action sessions require an actor")
        return [Decision(id_, session.observe(id_, session.actor)) for id_ in session.active()]

    def start(self) -> list[Decision] | results.Finished:
        try:
            self._session.start()
            return self._result()
        except BaseException:
            self.close()
            raise

    def advance(self, responses: list[DecisionActions]) -> list[Decision] | results.Finished:
        try:
            self._session.begin_actions(responses)
            for response in responses:
                for action in response.actions:
                    receipt = self._session.apply(response.rollout_id, action)
                    if isinstance(receipt.outcome, results.Rejected):
                        break
            self._session.close_month()
            return self._result()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._session.close()
