"""The real session posts opaque component effects without owning its model state."""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.action_projection import metric_arrays
from finance.augur.sim.actions import Action, Contribute, DecisionActions, Liquidate, Withdraw
from finance.augur.sim.books import AccountRef, TlhPortfolioState
from finance.augur.sim.compiler.execution import compile_run
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, quantity_scale_for_asset
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedAccount,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedJurisdiction,
    PreparedLot,
    PreparedSeries,
    PreparedTlhPortfolio,
)
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    Currency,
    InitialAccountBalance,
    InitialLot,
    InterestIncome,
    Scenario,
    TaxProfile,
    TlhPortfolioSpec,
)
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tlh import TlhAssumptions, TlhMarketUpdate, TlhPortfolio
from finance.augur.sim.world import Capture, World

ASSET = SecurityKey(symbol=SecuritySymbol("managed-index"))
SCALE = quantity_scale_for_asset(ASSET)
# Whole-dollar money, so a portfolio mark is the number the assertions name.
QUANTUM = Decimal(1)
OWNER = "owner"
OTHER = "other"
IRS = "irs"
CHECKING = "checking"
FEDERAL = "federal_us"


def ref(agent_id: str) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=CHECKING)


def assumptions(*, harvest: bool) -> TlhAssumptions:
    return TlhAssumptions(
        peak_annual_yield=0.12 if harvest else 0,
        floor_annual_yield=0,
        maturity_decay_exponent=1,
        drawdown_sensitivity=0,
        short_term_fraction=1,
    )


@dataclass(frozen=True)
class Situation:
    """One managed sleeve opened from an imported cohort, and who else exists beside its owner."""

    cash: int = 0
    horizon: int = 2
    rollouts: int = 1
    harvest: bool = True
    prices: tuple[int, ...] | None = None
    distributions: tuple[PreparedDistribution, ...] = ()
    interest_sources: tuple[InterestIncome, ...] = ()
    distribution_rates: tuple[Decimal, ...] = ()
    taxed: bool = True
    bystander: bool = False

    @property
    def snapshots(self) -> int:
        return self.horizon + 1

    def series(self) -> tuple[PreparedSeries, ...]:
        prices = self.prices if self.prices is not None else (1,) * self.snapshots
        rows = [
            PreparedSeries(
                series_id=f"security:{ASSET.symbol}", snapshots=self.snapshots, values=prices * self.rollouts
            )
        ]
        if self.distribution_rates:
            # Per-unit payouts in prepared rate units: money quanta on the money-factor grid.
            rows.append(
                PreparedSeries(
                    series_id=f"security_distribution:{ASSET.symbol}",
                    snapshots=self.snapshots,
                    values=tuple(int(rate * MONEY_FACTOR_SCALE) for rate in self.distribution_rates) * self.rollouts,
                )
            )
        return tuple(rows)


def cohort() -> PreparedLot:
    return PreparedLot(
        lot_id="imported",
        agent_id=OWNER,
        account_id=CHECKING,
        asset_id=str(ASSET.symbol),
        purchase_month=-24,
        quantity_scale=SCALE,
        units=100 * SCALE,
        basis=100,
    )


def compose(case: Situation, rollout_id: int) -> World:
    world = World(
        MarketPath(case.series(), rollout_id, rollout_count=case.rollouts),
        horizon_months=case.horizon,
        income_sources=(ORDINARY_INCOME, *case.interest_sources),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=FEDERAL, level=load_jurisdiction(FEDERAL).level),)
        if case.taxed
        else (),
    )
    for agent_id, balance in ((OWNER, case.cash), (OTHER if case.bystander else IRS, 0)):
        world.declare_account(PreparedAccount(account=ref(agent_id), opening_balance=balance))
    if case.taxed:
        world.track(
            TaxAuthority(
                compile_profile(
                    TaxProfile(agent_id=OWNER, jurisdiction_ids=[FEDERAL], tax_authority_agent_id=IRS),
                    {FEDERAL: load_jurisdiction(FEDERAL)},
                    quantum=QUANTUM,
                )
            )
        )
    world.declare_portfolio(
        PreparedTlhPortfolio(
            portfolio_id="managed",
            owner_agent_id=OWNER,
            account_id=CHECKING,
            asset_id=str(ASSET.symbol),
            quantity_scale=SCALE,
            initial_cohorts=(cohort(),),
            assumptions=assumptions(harvest=case.harvest),
        )
    )
    for distribution in case.distributions:
        world.declare_distribution(distribution)
    return world


def session(case: Situation, actor: str = OWNER, *, capture: Capture = "forensic") -> ActionSession:
    return ActionSession(
        {rollout_id: compose(case, rollout_id) for rollout_id in range(case.rollouts)}, actor, capture=capture
    )


def test_managed_opening_is_not_an_ordinary_lot_and_sale_follows_same_month_loss() -> None:
    live = session(Situation(horizon=1))
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        [decision] = batch
        assert decision.observation.public_positions == ()
        [portfolio] = decision.observation.tlh_portfolios
        assert (portfolio.value, portfolio.reported_tax_basis) == (100, 99)
        result = live.advance(
            [
                DecisionActions(
                    0, 0, [Liquidate(cause_id="sell", agent_id=OWNER, portfolio_id="managed", cash_account_id=CHECKING)]
                )
            ]
        )
        assert isinstance(result, Finished)
    finally:
        live.close()
    [rollout] = result.rollouts
    assert rollout.stop is None
    assert rollout.trace is not None
    assert rollout.trace.books[0].tlh_portfolios[0].reported_tax_basis == 100
    assert rollout.summary.ending_book.tlh_portfolios == [
        TlhPortfolioState(
            portfolio_id="managed",
            owner_agent_id=OWNER,
            account_id=CHECKING,
            asset_id=str(ASSET.symbol),
            value=0,
            reported_tax_basis=0,
        )
    ]
    assert rollout.summary.cash[0].values[-1] == 100
    # Modeled ST loss and actual LT gain are separately characterized, never a cash subsidy.
    [gain] = rollout.summary.ending_book.capital_gains
    assert (gain.short_term_gain, gain.long_term_gain) == (-1, 1)
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in rollout.trace.journal)


def test_rejected_contribution_preserves_harvest_and_earlier_withdrawal_without_future_policy_calls() -> None:
    live = session(Situation(rollouts=2))
    try:
        first = live.start()
        assert not isinstance(first, Finished)
        second = live.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Withdraw(
                            cause_id="cash", agent_id=OWNER, portfolio_id="managed", cash_account_id=CHECKING, amount=10
                        ),
                        Contribute(
                            cause_id="too-much",
                            agent_id=OWNER,
                            portfolio_id="managed",
                            cash_account_id=CHECKING,
                            amount=11,
                        ),
                        Liquidate(cause_id="never", agent_id=OWNER, portfolio_id="managed", cash_account_id=CHECKING),
                    ],
                ),
                DecisionActions(1, 0, []),
            ]
        )
        assert not isinstance(second, Finished)
        assert [(row.rollout_id, row.observation.month) for row in second] == [(1, 1)]
        result = live.advance([DecisionActions(1, 1, [])])
        assert isinstance(result, Finished)
    finally:
        live.close()
    stopped, alive = result.rollouts
    assert stopped.stop == RejectedAction(month=0, action_index=1)
    assert alive.stop is None
    assert stopped.trace is not None
    assert len(stopped.trace.books) == 2
    assert [type(receipt.outcome) for receipt in stopped.trace.receipts] == [Executed, Rejected]
    [portfolio] = stopped.summary.ending_book.tlh_portfolios
    assert (portfolio.value, portfolio.reported_tax_basis) == (90, 89)
    assert stopped.summary.cash[0].values == [0, 10]
    assert stopped.summary.ending_book.capital_gains[0].short_term_gain == -1


@pytest.mark.parametrize("amount", [-1, 101])
def test_invalid_withdrawal_changes_no_component_state(amount: int) -> None:
    live = session(Situation(horizon=1, harvest=False))
    try:
        live.start()
        result = live.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Withdraw(
                            cause_id="invalid",
                            agent_id=OWNER,
                            portfolio_id="managed",
                            cash_account_id=CHECKING,
                            amount=amount,
                        )
                    ],
                )
            ]
        )
        assert isinstance(result, Finished)
    finally:
        live.close()
    [rollout] = result.rollouts
    assert rollout.stop == RejectedAction(month=0, action_index=0)
    [portfolio] = rollout.summary.ending_book.tlh_portfolios
    assert (portfolio.value, portfolio.reported_tax_basis) == (100, 100)
    assert rollout.summary.cash[0].values[-1] == 0


def test_another_actors_component_is_neither_observed_nor_redeemable() -> None:
    live = session(Situation(horizon=1, harvest=False, taxed=False, bystander=True), OTHER)
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        assert batch[0].observation.tlh_portfolios == ()
        result = live.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [Liquidate(cause_id="steal", agent_id=OWNER, portfolio_id="managed", cash_account_id=CHECKING)],
                )
            ]
        )
        assert isinstance(result, Finished)
    finally:
        live.close()
    [rollout] = result.rollouts
    assert rollout.stop == RejectedAction(month=0, action_index=0)
    assert rollout.summary.ending_book.tlh_portfolios[0].value == 100


def test_model_defect_closes_session_instead_of_becoming_a_rejected_action(monkeypatch: pytest.MonkeyPatch) -> None:
    live = session(Situation())

    def broken_advance(self: TlhPortfolio, market: TlhMarketUpdate) -> None:
        raise ArithmeticError("model defect")

    monkeypatch.setattr(TlhPortfolio, "advance", broken_advance)
    with pytest.raises(ArithmeticError, match="model defect"):
        live.start()
    with pytest.raises(ValueError, match=r"closed|consumed|finished"):
        live.advance([DecisionActions(0, 0, [])])
    live.close()


def _scenario(*, horizon: int) -> Scenario:
    """The same managed sleeve as an authored scenario, for the prepared-input path below."""
    return Scenario(
        agents=[Agent(agent_id=OWNER), Agent(agent_id=IRS)],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id=CHECKING, balance=Decimal(0))
            for agent_id in (OWNER, IRS)
        ],
        tax_profiles=[TaxProfile(agent_id=OWNER, jurisdiction_ids=[FEDERAL], tax_authority_agent_id=IRS)],
        horizon_months=horizon,
        currency=Currency(quantum=QUANTUM),
        tlh_portfolios=[
            TlhPortfolioSpec(
                portfolio_id="managed",
                owner_agent_id=OWNER,
                account_id=CHECKING,
                asset=ASSET,
                initial_lots=[
                    InitialLot(
                        lot_id="imported",
                        agent_id=OWNER,
                        account_id=CHECKING,
                        asset=ASSET,
                        purchase_month_index=-24,
                        quantity=100.0,
                        cost_basis=Decimal(100),
                    )
                ],
                assumptions=assumptions(harvest=True),
            )
        ],
    )


def _projection_run(prices: tuple[float, ...]) -> CompiledRun:
    """`metric_arrays` reads a `CompiledRun`, so this one assertion keeps the prepared-input path."""
    horizon = len(prices) - 1
    return compile_run(
        _scenario(horizon=horizon),
        rollout_count=1,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(ASSET, np.asarray([prices], dtype=np.float64))], rollout_count=1, horizon_months=horizon
        ),
        jurisdictions={FEDERAL: load_jurisdiction(FEDERAL)},
        locations={},
    )


@pytest.mark.parametrize("capture", ["summary", "dense", "forensic"])
@pytest.mark.parametrize("reject", [False, True])
def test_closing_marks_and_product_projection_do_not_advance_the_model_early(capture: Capture, reject: bool) -> None:
    run = _projection_run((1.0, 2.0))
    live = ActionSession.from_run(run, OWNER, [0], capture=capture)
    try:
        live.start()
        actions: list[Action] = (
            [
                Withdraw(
                    cause_id="unfundable", agent_id=OWNER, portfolio_id="managed", cash_account_id=CHECKING, amount=101
                )
            ]
            if reject
            else []
        )
        result = live.advance([DecisionActions(0, 0, actions)])
        assert isinstance(result, Finished)
    finally:
        live.close()
    [rollout] = result.rollouts
    [portfolio] = rollout.summary.ending_book.tlh_portfolios
    assert portfolio.value == (100 if reject else 200)
    assert portfolio.reported_tax_basis == 99
    assert rollout.summary.ending_book.capital_gains[0].short_term_gain == -1
    metrics = metric_arrays(run, result.rollouts, primary_agent_id=OWNER)
    assert metrics.base_series[1][:, 0].tolist() == [100, 100 if reject else 200]


def test_managed_subquantum_distribution_keeps_cash_and_issuer_character() -> None:
    case = Situation(
        horizon=1,
        harvest=False,
        distribution_rates=(Decimal("0.015"), Decimal(0)),
        distributions=(
            PreparedDistribution(
                agent_id=OWNER,
                holding_account_id=CHECKING,
                asset_id=str(ASSET.symbol),
                to_account_id=CHECKING,
                tax_character=(
                    PreparedDistributionSlice(fraction_ppb=500_000_000, issuer_jurisdiction_id=FEDERAL),
                    PreparedDistributionSlice(fraction_ppb=500_000_000, issuer_jurisdiction_id=None),
                ),
            ),
        ),
        interest_sources=(InterestIncome(issuer_jurisdiction_id=FEDERAL), InterestIncome(issuer_jurisdiction_id=None)),
    )
    live = session(case)
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        assert batch[0].observation.cash == 2  # round(100 × $0.015), then two $1 tax slices
        result = live.advance([DecisionActions(0, 0, [])])
        assert isinstance(result, Finished)
    finally:
        live.close()
    [rollout] = result.rollouts
    assert rollout.trace is not None
    assert [(row.issuer_jurisdiction_id, row.units, row.amount) for row in rollout.trace.distributions] == [
        (FEDERAL, None, 1),
        (None, None, 1),
    ]
    assert rollout.summary.ending_book.tlh_portfolios[0].value == 100
    assert {(row.income_source, row.income) for row in rollout.summary.ending_book.income} == {
        ("ordinary", 0),  # The income ledger retains every declared source, including zero buckets.
        ("interest:federal_us", 1),
        ("interest:corporate", 1),
    }


def test_contribution_is_first_harvested_in_the_next_month() -> None:
    live = session(Situation(cash=100))
    try:
        first = live.start()
        assert not isinstance(first, Finished)
        assert first[0].observation.tlh_portfolios[0].reported_tax_basis == 99
        second = live.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Contribute(
                            cause_id="new", agent_id=OWNER, portfolio_id="managed", cash_account_id=CHECKING, amount=100
                        )
                    ],
                )
            ]
        )
        assert not isinstance(second, Finished)
        assert second[0].observation.tlh_portfolios[0].reported_tax_basis == 197
        result = live.advance([DecisionActions(0, 1, [])])
        assert isinstance(result, Finished)
    finally:
        live.close()
    [rollout] = result.rollouts
    assert rollout.trace is not None
    assert rollout.trace.books[1].tlh_portfolios[0].reported_tax_basis == 199
    assert rollout.summary.ending_book.capital_gains[0].short_term_gain == -3


def test_removed_or_misplaced_fields_cannot_silently_disable_the_model() -> None:
    scenario = _scenario(horizon=2)
    for name, value in (("harvest_policies", []), ("currency_quantum", "1")):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Scenario.model_validate({**scenario.model_dump(), name: value})
    [portfolio] = scenario.tlh_portfolios
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TlhPortfolioSpec.model_validate({**portfolio.model_dump(), "cumulative_harvest": 1})


if __name__ == "__main__":
    pytest_bazel.main()
