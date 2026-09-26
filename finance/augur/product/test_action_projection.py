"""One executed session supplies the product's wealth, payment, sale and tax views.

Stipulated $100/$50 share prices, $40 basis and synthetic 10% LTCG tax come from
the runnable monthly-actions example; these are accounting controls, not forecasts.
"""

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from functools import partial
from typing import Literal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.product.action_projection import metric_arrays
from finance.augur.product.metrics import OutcomeBasis, ProductMetricArrays, projection_summaries
from finance.augur.product.projection import ProductRolloutProjection, project_product_rollout
from finance.augur.product.wire import HoldingSaleEvent, MonthlyExpenseEvent, RolloutFailureEvent, TaxAccrualEvent
from finance.augur.sim.actions import Consume, DecisionActions
from finance.augur.sim.books import AccountRef
from finance.augur.sim.events import EventLog
from finance.augur.sim.ids import AccountId, AgentId, BondId
from finance.augur.sim.observations import Decision
from finance.augur.sim.prepared import PreparedAccount, PreparedBond
from finance.augur.sim.results import Finished, PaymentRejection, PaymentRequestError, Rejected, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.bonds import (
    CPI_DOUBLING,
    HORIZON,
    Situation,
    bond_case,
    checking,
    compose,
    cpi_series,
    dated,
)
from finance.augur.sim.world import World
from finance.augur.x.monthly_actions.policy import decide

# Aliased: the bond situations' `compose` holds the plain name.
from finance.augur.x.monthly_actions.run import HOUSEHOLD, STOCK, compose as example_world, situation

type Compose = Callable[[int], World]
EXAMPLE_HORIZON = 13


def _run(
    worlds: Compose,
    ids: list[int],
    capture: Literal["summary", "dense", "forensic"],
    policy: Callable[[list[Decision]], list[DecisionActions]] = decide,
    *,
    actor_id: AgentId = HOUSEHOLD,
) -> list[Rollout]:
    session = ActionSession({id_: worlds(id_) for id_ in ids}, actor_id, capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts
    finally:
        session.close()


def _metrics(
    rollouts: list[Rollout], *, horizon_months: int = EXAMPLE_HORIZON, primary_agent_id: AgentId = HOUSEHOLD
) -> ProductMetricArrays:
    return metric_arrays(
        rollouts,
        primary_agent_id=primary_agent_id,
        horizon_months=horizon_months,
        currency_code="USD",
        currency_quantum="0.01",
    )


def _detail(rollouts: list[Rollout], column: int, *, horizon_months: int = EXAMPLE_HORIZON) -> ProductRolloutProjection:
    trace = rollouts[column].trace
    assert trace is not None
    return project_product_rollout(
        trace.events,
        _metrics(rollouts, horizon_months=horizon_months),
        rollout_id=rollouts[column].rollout_id,
        primary_agent_id=HOUSEHOLD,
        asset_labels={STOCK: "Stipulated stock"},
    )


def _bonds(
    *bonds: PreparedBond,
    horizon_months: int,
    accounts: tuple[PreparedAccount, ...] = checking(
        (HOUSEHOLD, Decimal(0)), (AgentId("example-creditor"), Decimal(0))
    ),
    cpi: list[list[float]] | None = None,
    rollout_count: int = 1,
) -> Compose:
    """The example household and its creditor holding `bonds`, one path per CPI path when indexed; nothing taxed."""
    case = Situation(
        accounts=accounts,
        bonds=bonds,
        horizon_months=horizon_months,
        series=() if cpi is None else cpi_series(cpi),
        rollout_count=rollout_count if cpi is None else len(cpi),
    )
    return partial(compose, case)


@pytest.fixture(scope="module")
def example() -> Compose:
    return partial(example_world, situation(rollout_count=5))


@pytest.fixture(scope="module")
def outcomes(example: Compose) -> dict[str, list[Rollout]]:
    captures: tuple[Literal["summary", "dense", "forensic"], ...] = ("summary", "dense", "forensic")
    return {capture: _run(example, [0, 1], capture) for capture in captures}


def test_same_session_events_and_metrics_reconcile_sales_receipts_and_tax(
    example: Compose, outcomes: dict[str, list[Rollout]]
) -> None:
    for capture in ("dense", "forensic"):
        rollouts = outcomes[capture]
        funded = _detail(rollouts, 0)
        assert funded.failed_month_index is None
        assert funded.monthly_metric_arrays["cash_quanta"].tolist() == [0] + [5_000] * 12 + [3_800]
        assert funded.monthly_metric_arrays["holding_value_quanta"].tolist() == [20_000] + [0] * 13
        sales = [event for event in funded.events if isinstance(event, HoldingSaleEvent)]
        assert [(sale.month_index, sale.proceeds_quanta, sale.cost_basis_quanta) for sale in sales] == [
            (0, "20000", "8000")
        ]
        taxes = [event for event in funded.events if isinstance(event, TaxAccrualEvent)]
        assert [(tax.month_index, tax.amount_quanta) for tax in taxes] == [(11, "1200")]
        summary = rollouts[0].summary
        assert [(row.month, row.amount_paid) for row in summary.tax_payments] == [(12, 1_200)]
        assert sum(row.receipt.amount_requested for row in summary.payments) == 16_200
        assert funded.monthly_metric_arrays["cash_quanta"][-1] == 20_000 - 16_200
        stopped = _detail(rollouts, 1)
        assert stopped.failed_month_index == 0
        assert stopped.monthly_metric_arrays["cash_quanta"].tolist() == [0, 10_000]
        failures = [event for event in stopped.events if isinstance(event, RolloutFailureEvent)]
        # The whole bill is unpaid, not merely the extra $50 needed to fund it.
        assert [event.shortfall_quanta for event in failures] == ["15000"]
        assert stopped.monthly_metric_arrays["shortfall_quanta"].tolist() == [0, 15_000]


def test_compact_population_and_selected_detail_share_observed_support(
    example: Compose, outcomes: dict[str, list[Rollout]]
) -> None:
    compact = _metrics(outcomes["summary"])
    assert all(row.trace is None for row in outcomes["summary"])
    for capture in ("dense", "forensic"):
        detailed = _metrics(outcomes[capture])
        for detailed_series, compact_series in zip(detailed.base_series, compact.base_series, strict=True):
            np.testing.assert_array_equal(detailed_series, compact_series)
    wealth = projection_summaries(compact, metric="net_worth_quanta", percentiles=(0, 50, 100))
    assert wealth.metric_fan.observed_count.tolist() == [2] + [1] * 13
    assert wealth.terminal_distribution.observed.tolist() == [True, False]
    assert wealth.metric_fan.terminal_percentiles is not None
    assert wealth.metric_fan.terminal_percentiles.tolist() == [3_800] * 3
    shortfall = projection_summaries(compact, metric="shortfall_quanta", percentiles=(0, 50, 100))
    assert shortfall.terminal_distribution.basis == OutcomeBasis.OBSERVED_THROUGH_STOP
    assert shortfall.terminal_distribution.terminal_samples.tolist() == [0, 15_000]
    assert shortfall.metric_fan.observed_count.tolist() == [2, 2] + [1] * 12
    for ids in ([1, 0], [1], [0]):
        replay = _run(example, ids, "dense")
        assert replay == [outcomes["dense"][id_] for id_ in ids]
        for column, id_ in enumerate(ids):
            actual = _detail(replay, column)
            expected = _detail(outcomes["dense"], id_)
            assert actual.events == expected.events
            for name, series in expected.monthly_metric_arrays.items():
                np.testing.assert_array_equal(actual.monthly_metric_arrays[name], series)


def test_attempted_consumption_gap_does_not_duplicate_claims_or_invent_future_demand(example: Compose) -> None:
    def consume_after_bills(batch: list[Decision]) -> list[DecisionActions]:
        responses = decide(batch)
        return [
            DecisionActions(
                response.rollout_id,
                response.month,
                response.actions
                + [
                    Consume(
                        request_id=i,
                        cause_id=f"spend-{i}",
                        component_id="budget",
                        from_account=AccountRef(
                            agent_id=AgentId("example-household"), account_id=AccountId("checking")
                        ),
                        to_account=AccountRef(agent_id=AgentId("example-creditor"), account_id=AccountId("checking")),
                        amount=amount,
                    )
                    for i, amount in enumerate((2_000, 50_000, 1))
                ],
            )
            for response in responses
        ]

    for capture in ("summary", "dense", "forensic"):
        rollouts = _run(example, [0, 1], capture, consume_after_bills)
        metrics = _metrics(rollouts)
        assert metrics.metric_arrays()["shortfall_quanta"][1].tolist() == [50_000, 15_000]
        assert metrics.metric_arrays()["cash_quanta"][1].tolist() == [3_000, 10_000]
        assert rollouts[0].summary.unpaid_claims == []
        assert len(rollouts[1].summary.unpaid_claims) == 1
        if capture != "summary":
            details = _detail(rollouts, 0)
            consumed = [event for event in details.events if isinstance(event, MonthlyExpenseEvent)]
            assert [(event.amount_due_quanta, event.amount_paid_quanta) for event in consumed] == [
                ("2000", "2000"),
                ("50000", "0"),
            ]
            assert [event.shortfall_quanta for event in details.events if isinstance(event, RolloutFailureEvent)] == [
                "50000"
            ]


@pytest.mark.parametrize("invalid_amount", [-1, 0])
def test_malformed_consume_is_a_stop_not_a_monetary_shortfall(example: Compose, invalid_amount: int) -> None:
    def invalid(batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                decision.rollout_id,
                decision.observation.month,
                [
                    Consume(
                        request_id=0,
                        cause_id="invalid",
                        component_id="budget",
                        from_account=AccountRef(
                            agent_id=AgentId("example-household"), account_id=AccountId("checking")
                        ),
                        to_account=AccountRef(agent_id=AgentId("example-creditor"), account_id=AccountId("checking")),
                        amount=invalid_amount,
                    )
                ],
            )
            for decision in batch
        ]

    rollouts = _run(example, [0], "dense", invalid)
    details = _detail(rollouts, 0)
    assert details.failed_month_index == 0
    assert not any(isinstance(event, MonthlyExpenseEvent) for event in details.events)
    # No consumption was incurred; the pre-existing bill remains due exactly once.
    assert details.monthly_metric_arrays["shortfall_quanta"].tolist() == [0, 15_000]
    assert rollouts[0].summary.last_receipts[0].outcome == Rejected(
        reason=PaymentRejection(detail=PaymentRequestError(kind="InvalidAmount"))
    )


def test_projection_rejects_mismatched_actor_and_duplicate_selection(example: Compose) -> None:
    rollouts = _run(example, [1], "summary")
    with pytest.raises(ValueError, match="result actor"):
        _metrics(rollouts, primary_agent_id=AgentId("example-creditor"))
    with pytest.raises(ValueError, match="unique selection"):
        _metrics(rollouts * 2)


def test_original_ids_own_columns_through_noncontiguous_selection(example: Compose) -> None:
    source = _run(example, [0, 1, 2, 3, 4], "dense")
    selected = _run(example, [4, 1, 2], "dense")
    population = _metrics(selected)
    assert population.rollout_ids == (4, 1, 2)
    subset = population.select((1, 4))
    assert subset.rollout_ids == (1, 4)
    assert subset.failed_month.tolist() == [0, -1]
    for rollout_id in subset.rollout_ids:
        trace = source[rollout_id].trace
        assert trace is not None
        events = trace.events
        assert events.rollout_ids == (rollout_id,)
        assert events.lot_dispositions.get_column("rollout_id").unique().to_list() == [rollout_id]
        actual = project_product_rollout(
            events,
            subset,
            rollout_id=rollout_id,
            primary_agent_id=AgentId("example-household"),
            asset_labels={STOCK: "Stipulated stock"},
        )
        expected = _detail(source, rollout_id)
        assert actual.rollout_id == expected.rollout_id == rollout_id
        assert actual.events == expected.events
        for name, values in actual.monthly_metric_arrays.items():
            np.testing.assert_array_equal(values, expected.monthly_metric_arrays[name])
    with pytest.raises(ValueError, match="undeclared rollout ID"):
        EventLog.from_frames({"lot_dispositions": events.lot_dispositions}, rollout_ids=(1,))
    wrong_trace = source[1].trace
    assert wrong_trace is not None
    with pytest.raises(ValueError, match="both metric and event"):
        project_product_rollout(
            wrong_trace.events, subset, rollout_id=4, primary_agent_id=AgentId("example-household"), asset_labels={}
        )
    with pytest.raises(ValueError, match="unknown metric rollout IDs"):
        population.select((0,))
    with pytest.raises(ValueError, match="unique"):
        population.select((4, 4))
    with pytest.raises(ValueError, match="failure vector"):
        replace(population, rollout_ids=(4, 1))


def test_eventless_trace_keeps_its_owner_and_rejects_another_paths_metrics() -> None:
    worlds = _bonds(horizon_months=2, accounts=checking((HOUSEHOLD, Decimal(10))), rollout_count=8)

    def no_actions(batch: list[Decision]) -> list[DecisionActions]:
        return [DecisionActions(row.rollout_id, row.observation.month, []) for row in batch]

    rollouts = _run(worlds, [7, 2], "dense", no_actions)
    population = _metrics(rollouts, horizon_months=2)
    trace = rollouts[0].trace
    assert trace is not None
    events = trace.events
    assert events.rollout_ids == (7,)
    assert events.transfers.is_empty()
    detail = project_product_rollout(
        events, population, rollout_id=7, primary_agent_id=AgentId("example-household"), asset_labels={}
    )
    assert detail.events == ()
    assert detail.rollout_id == 7
    assert detail.monthly_metric_arrays["cash_quanta"].tolist() == [1_000] * 3
    with pytest.raises(ValueError, match="both metric and event"):
        project_product_rollout(
            events, population, rollout_id=2, primary_agent_id=AgentId("example-household"), asset_labels={}
        )


def _hold(batch: list[Decision]) -> list[DecisionActions]:
    return [DecisionActions(row.rollout_id, row.observation.month, []) for row in batch]


def test_product_net_worth_carries_indexed_bonds_at_indexed_principal() -> None:
    # Preserve the actual product metric control formerly in rust/backend_test.py.
    # Untaxed: tax on $1M accretion would stop this cash-constrained case first.
    for indexed, expected in ((True, 200_000_000), (False, 100_000_000)):
        worlds = partial(compose, bond_case(indexed=indexed, cpi=CPI_DOUBLING, is_taxed=False))
        rollouts = _run(worlds, [0], "summary", _hold, actor_id=AgentId("alice"))
        metrics = _metrics(rollouts, horizon_months=HORIZON, primary_agent_id=AgentId("alice")).metric_arrays()
        assert rollouts[0].stop is None
        assert metrics["bond_value_quanta"][-1, 0] == expected
        assert metrics["net_worth_quanta"][-1, 0] == expected + metrics["cash_quanta"][-1, 0]


@pytest.mark.parametrize("capture", ["summary", "dense", "forensic"])
def test_redemption_replaces_principal_with_cash_without_changing_product_net_worth(
    capture: Literal["summary", "dense", "forensic"],
) -> None:
    worlds = _bonds(
        dated(BondId("test-note"), agent_id=HOUSEHOLD, face=Decimal(100), annual_rate=0, period=1, maturity=1),
        horizon_months=2,
    )
    rollouts = _run(worlds, [0], capture, _hold)
    assert rollouts[0].stop is None
    ending_bonds = rollouts[0].summary.ending_book.bonds
    assert ending_bonds is not None
    assert [bond.active for bond in ending_bonds] == [False]
    assert rollouts[0].summary.cash[0].values[-1] == 10_000
    metrics = _metrics(rollouts, horizon_months=2).metric_arrays()
    assert metrics["cash_quanta"][:, 0].tolist() == [0, 0, 10_000]
    assert metrics["bond_value_quanta"][:, 0].tolist() == [10_000, 10_000, 0]
    assert metrics["net_worth_quanta"][:, 0].tolist() == [10_000, 10_000, 10_000]
    [rollout] = rollouts
    [captured] = rollout.summary.bond_principal
    # Even a fully redeemed position needs its actual pre-redemption history.
    for rows in (
        [],
        [captured, captured],
        [captured.model_copy(update={"bond_id": "other"})],
        [captured.model_copy(update={"account": captured.account.model_copy(update={"account_id": "other"})})],
    ):
        invalid = rollout.model_copy(update={"summary": rollout.summary.model_copy(update={"bond_principal": rows})})
        with pytest.raises(ValueError, match="held-bond principal history"):
            _metrics([invalid], horizon_months=2)


def test_bond_principal_totals_named_accounts_without_another_actors_holdings() -> None:
    worlds = _bonds(
        *(
            dated(
                BondId(f"{actor}-{account}"),
                agent_id=actor,
                account_id=account,
                face=Decimal(face),
                annual_rate=0,
                period=1,
                maturity=1,
            )
            for actor, account, face in (
                (HOUSEHOLD, AccountId("checking"), 100),
                (HOUSEHOLD, AccountId("savings"), 75),
                (AgentId("example-creditor"), AccountId("checking"), 999),
            )
        ),
        horizon_months=1,
        accounts=(
            *checking((HOUSEHOLD, Decimal(0)), (AgentId("example-creditor"), Decimal(0))),
            PreparedAccount(account=AccountRef(agent_id=HOUSEHOLD, account_id=AccountId("savings")), opening_balance=0),
        ),
    )
    rollouts = _run(worlds, [0], "summary", _hold)
    assert len(rollouts[0].summary.bond_principal) == 2
    values = _metrics(rollouts, horizon_months=1).metric_arrays()
    assert values["bond_value_quanta"][:, 0].tolist() == [17_500, 17_500]
    assert values["net_worth_quanta"][:, 0].tolist() == [17_500, 17_500]


def test_stopped_bond_marks_and_selected_replay_use_captured_cpi_not_future_values() -> None:
    worlds = _bonds(
        dated(
            BondId("indexed-note"),
            agent_id=HOUSEHOLD,
            face=Decimal(100),
            annual_rate=0,
            period=1,
            maturity=2,
            indexed=True,
        ),
        horizon_months=3,
        cpi=[[1.0, 2.0, 99.0, 99.0], [1.0, 2.0, 3.0, 4.0]],
    )

    def stop_first(batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                row.rollout_id,
                row.observation.month,
                [
                    Consume(
                        request_id=0,
                        cause_id="unfunded",
                        component_id="budget",
                        from_account=AccountRef(
                            agent_id=AgentId("example-household"), account_id=AccountId("checking")
                        ),
                        to_account=AccountRef(agent_id=AgentId("example-creditor"), account_id=AccountId("checking")),
                        amount=1,
                    )
                ]
                if row.rollout_id == 0 and row.observation.month == 1
                else [],
            )
            for row in batch
        ]

    baseline = _run(worlds, [0, 1], "summary", stop_first)
    compact = _metrics(baseline, horizon_months=3)
    arrays = compact.metric_arrays()
    assert arrays["bond_value_quanta"].tolist() == [[10_000, 10_000], [20_000, 20_000], [20_000, 30_000], [0, 0]]
    assert arrays["cash_quanta"].tolist() == [[0, 0], [0, 0], [0, 0], [0, 30_000]]
    assert compact.failed_month.tolist() == [1, -1]
    assert compact.observed[:, 0].tolist() == [True, True, True, False]
    wealth = projection_summaries(compact, metric="net_worth_quanta", percentiles=(0, 50, 100))
    assert wealth.metric_fan.observed_count.tolist() == [2, 2, 1, 1]
    assert wealth.terminal_distribution.observed.tolist() == [False, True]
    assert wealth.metric_fan.terminal_percentiles is not None
    assert wealth.metric_fan.terminal_percentiles.tolist() == [30_000] * 3
    for capture in ("dense", "forensic"):
        selected = _run(worlds, [1, 0], capture, stop_first)
        detailed = _metrics(selected, horizon_months=3)
        assert detailed.rollout_ids == (1, 0)
        for actual, expected in zip(detailed.base_series, compact.select((1, 0)).base_series, strict=True):
            np.testing.assert_array_equal(actual, expected)
        stopped = selected[1]
        assert stopped.summary.ending_mark_month == 1
        detail = _detail(selected, 1, horizon_months=3)
        assert detail.monthly_metric_arrays["bond_value_quanta"].tolist() == [10_000, 20_000, 20_000]


if __name__ == "__main__":
    pytest_bazel.main()
