"""Python-owned monthly orchestration with one ordered batch policy response per month."""

from copy import deepcopy
from typing import Literal

from finance.augur.rust import _simulator as native
from finance.augur.rust._simulator import (
    Action,
    Claim,
    Decision,
    DecisionActions,
    FixedCoupon,
    HeldBond,
    HoldingPool,
    IndexedCoupon,
    Observation,
    PublicPosition,
    TlhPortfolioObservation,
)
from finance.augur.sim import results
from finance.augur.sim.prepared import CompiledRun, PreparedTlhPortfolio
from finance.augur.sim.tlh import (
    ModeledRealizations,
    TlhMarketUpdate,
    TlhObservation,
    TlhOpening,
    TlhOpeningPosition,
    TlhPortfolio,
)

type Capture = Literal["summary", "dense", "forensic"]

__all__ = [
    "Action",
    "ActionSession",
    "Capture",
    "Claim",
    "Decision",
    "DecisionActions",
    "FixedCoupon",
    "HeldBond",
    "HoldingPool",
    "IndexedCoupon",
    "Observation",
    "PublicPosition",
    "TlhPortfolioObservation",
]

_NO_REALIZATIONS = ModeledRealizations()


class _Session:
    """Shared phase and component ownership for action and remaining configured callers."""

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
        self.run = run
        self.actor = actor
        self.configured = configured
        self.specs = {spec.portfolio_id: spec for spec in run.scenario.tlh_portfolios}
        self.series = {series.series_id: series for series in run.series}
        self.portfolios = {
            rollout_id: {
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
                for spec in run.scenario.tlh_portfolios
            }
            for rollout_id in rollout_ids
        }
        opening = [
            (
                rollout_id,
                [
                    self.statement(spec, self.portfolios[rollout_id][spec.portfolio_id].observe())
                    for spec in self.specs.values()
                ],
            )
            for rollout_id in rollout_ids
        ]
        self.native = native._NativeSession(
            run, actor, rollout_ids, opening, capture=capture, configured=configured, product_actor=product_actor
        )

    def price(self, series_id: str, rollout_id: int, month: int) -> int:
        series = self.series[series_id]
        return series.values[rollout_id * series.snapshots + month]

    @staticmethod
    def statement(spec: PreparedTlhPortfolio, value: TlhObservation) -> TlhPortfolioObservation:
        return TlhPortfolioObservation(
            spec.portfolio_id,
            spec.owner_agent_id,
            spec.account_id,
            spec.asset_id,
            value.value,
            value.reported_tax_basis,
        )

    def effects(
        self,
        spec: PreparedTlhPortfolio,
        candidate: TlhPortfolio,
        cash_account_id: str | None,
        cash_amount: int,
        realizations: ModeledRealizations = _NO_REALIZATIONS,
    ) -> native.ComponentEffects:
        return native.ComponentEffects(
            self.statement(spec, candidate.observe()),
            cash_account_id,
            cash_amount,
            realizations.short_term_gain,
            realizations.long_term_gain,
            [],
        )

    def start(self) -> None:
        self.native.start()
        self.opening()

    def opening(self) -> None:
        """The manager advances before any investor operation, including configured sales."""
        for status in self.native.current_paths():
            rollout_id = status.rollout_id
            month = status.month
            for spec in self.specs.values():
                current = self.portfolios[rollout_id][spec.portfolio_id]
                for index, distribution in enumerate(self.run.scenario.distributions):
                    if (distribution.agent_id, distribution.holding_account_id, distribution.asset_id) != (
                        spec.owner_agent_id,
                        spec.account_id,
                        spec.asset_id,
                    ):
                        continue
                    rate = self.price(f"security_distribution:{spec.asset_id}", rollout_id, month)
                    amount = current._distribution(rate)
                    self.native.component_distribution(rollout_id, index, amount)
                candidate = deepcopy(current)
                realized = candidate.advance(
                    TlhMarketUpdate(month, self.price(f"security:{spec.asset_id}", rollout_id, month))
                )
                self.native.apply_component(
                    rollout_id,
                    f"tlh:{spec.portfolio_id}:advance:m{month}",
                    self.effects(spec, candidate, None, 0, realized),
                )
                self.portfolios[rollout_id][spec.portfolio_id] = candidate

    def apply(self, rollout_id: int, action: Action) -> results.Receipt:
        request = action.request
        if not isinstance(request, results.Contribute | results.Withdraw | results.Liquidate):
            return self.native.apply(rollout_id, action)
        spec = self.specs.get(request.portfolio_id)
        if (
            spec is None
            or request.agent_id != spec.owner_agent_id
            or (not self.configured and request.agent_id != self.actor)
        ):
            return self.native.reject(rollout_id, action, "unknown or unowned TLH portfolio")
        if not request.cause_id:
            return self.native.reject(rollout_id, action, "TLH cause identifier must not be empty")
        available = self.native.account_balance(rollout_id, request.agent_id, request.cash_account_id)
        if available is None:
            return self.native.reject(rollout_id, action, "unknown TLH cash account")
        current = self.portfolios[rollout_id][spec.portfolio_id]
        if isinstance(request, results.Contribute | results.Withdraw):
            if request.amount < 0:
                return self.native.reject(rollout_id, action, "TLH amount must be nonnegative")
            if isinstance(request, results.Contribute) and request.amount > available:
                return self.native.reject(rollout_id, action, "TLH contribution exceeds available cash")
            if isinstance(request, results.Withdraw) and request.amount > current.observe().value:
                return self.native.reject(rollout_id, action, "TLH withdrawal exceeds portfolio value")
        candidate = deepcopy(current)
        if isinstance(request, results.Contribute):
            contribution = candidate.contribute(request.amount)
            effects = self.effects(spec, candidate, request.cash_account_id, -contribution.cash_paid)
        else:
            withdrawal = (
                candidate.withdraw(request.amount) if isinstance(request, results.Withdraw) else candidate.liquidate()
            )
            effects = self.effects(
                spec, candidate, request.cash_account_id, withdrawal.cash_received, withdrawal.realizations
            )
        receipt = self.native.apply_component(rollout_id, request.cause_id, effects, action)
        if receipt is None:
            raise RuntimeError("an investor operation must return a receipt")
        if isinstance(receipt.outcome, results.Executed):
            self.portfolios[rollout_id][spec.portfolio_id] = candidate
        return receipt

    def close_month(self) -> None:
        statuses = self.native.end_actions()
        self.native.set_component_marks(
            [
                (
                    status.rollout_id,
                    [
                        self.statement(
                            spec,
                            self.portfolios[status.rollout_id][spec.portfolio_id]._observe_at_price(
                                self.price(
                                    f"security:{spec.asset_id}",
                                    status.rollout_id,
                                    status.month + (0 if status.stopped else 1),
                                )
                            ),
                        )
                        for spec in self.specs.values()
                    ],
                )
                for status in statuses
            ]
        )
        self.native.close_month()
        if not self.native.is_finished():
            self.opening()


class ActionSession:
    """One household's batch session; only the caller invokes policy code.

    Submit one keyed ordered response per current path. Rejection stops that path,
    preserving earlier effects; no retries or engine-selected rescue actions occur.
    """

    def __init__(self, run: CompiledRun, actor: str, rollout_ids: list[int], *, capture: Capture = "forensic") -> None:
        self._session = _Session(run, actor, rollout_ids, capture=capture)

    def _result(self) -> list[Decision] | results.Finished:
        if self._session.native.is_finished():
            return self._session.native.finish()
        return self._session.native.observations()

    def start(self) -> list[Decision] | results.Finished:
        try:
            self._session.start()
            return self._result()
        except BaseException:
            self.close()
            raise

    def advance(self, responses: list[DecisionActions]) -> list[Decision] | results.Finished:
        try:
            self._session.native.begin_actions(responses)
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
        self._session.native.close()
