"""Batch/scalar authoring, exact rounding, stopped prefixes and selected action replay."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.rust.invocation import write_prepared_input
from finance.augur.rust.simulator import ActionSession, DecisionActions
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.results import Finished, Paid, RejectedAction, Rollout
from finance.augur.sim.scenario import (
    FilingStatus,
    InitialLot,
    ObligationType,
    ScheduledObligation,
    ScheduledTransfer,
    TaxProfile,
)
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking
from finance.augur.x.bounded_spending.python_policy import (
    BatchPolicy,
    Observation,
    Observations,
    Parameters,
    ScalarAdapter,
    ScalarPolicy,
    SpendingPolicy,
    consumption,
    run,
)
from finance.augur.x.bounded_spending.stress_paths import prepare
from util.bazel.runfiles import get_required_path


@pytest.fixture(params=[Parameters(400, 1000, 500), Parameters(400, 0, 0)])
def control(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Parameters, str, Finished, list[Rollout]]:
    parameters: Parameters = request.param
    prepared = prepare(rollout_count=3, horizon_months=36)
    path = tmp_path / "input.json"
    write_prepared_input(prepared, path)
    targets = {("brokerage", "STOCKS"): 1}
    baseline = run(path.read_text(), SpendingPolicy(BatchPolicy(parameters, 3), targets), [0, 1, 2])
    traces = run(path.read_text(), SpendingPolicy(BatchPolicy(parameters, 3), targets), [0, 1, 2], capture="forensic")
    return (parameters, path.read_text(), baseline, traces.rollouts)


@pytest.mark.parametrize("batch_authored", [False, True])
@pytest.mark.parametrize("chunk_size", [None, 1, 2])
def test_scalar_adapter_and_batch_authoring_preserve_path_identity(
    control: tuple[Parameters, str, Finished, list[Rollout]], batch_authored: bool, chunk_size: int | None
) -> None:
    parameters, input_json, baseline, traces = control
    ids = [0, 1, 2]
    policy = BatchPolicy(parameters, 3) if batch_authored else ScalarAdapter(parameters, ids)
    assert (
        run(input_json, SpendingPolicy(policy, {("brokerage", "STOCKS"): 1}), ids, chunk_size=chunk_size, reverse=True)
        == baseline
    )
    if chunk_size is None:
        for id_ in reversed(ids):
            replay_policy = BatchPolicy(parameters, 3) if batch_authored else ScalarAdapter(parameters, [id_])
            trace = run(
                input_json, SpendingPolicy(replay_policy, {("brokerage", "STOCKS"): 1}), [id_], capture="forensic"
            )
            assert trace.rollouts == [traces[id_]]
        second_year = [5_250_000, 4_500_000, 4_992_000] if parameters.max_cut_bps else [5_000_000] * 3
        assert [row[12] for row in consumption(baseline)[1]] == second_year


@pytest.mark.parametrize("batch_authored", [False, True])
def test_depleted_paths_stop_and_live_zero_requests_continue(batch_authored: bool) -> None:
    input_json = json.dumps(prepare(rollout_count=3, horizon_months=36).execution_input)
    for cut, expected_length, failure in [(0, 13, 12), (10_000, 36, -1)]:
        parameters = Parameters(10_000, cut, 0)
        policy = BatchPolicy(parameters, 3) if batch_authored else ScalarAdapter(parameters, [0, 1, 2])
        result = run(input_json, SpendingPolicy(policy, {("brokerage", "STOCKS"): 1}), [0, 1, 2])
        assert [row.summary.ending_mark_month if row.stop is not None else -1 for row in result.rollouts] == [
            failure
        ] * 3
        paid = consumption(result)[1]
        assert all(len(path) == expected_length for path in paid)
        assert all(path == [100_000_000, *([0] * (expected_length - 1))] for path in paid)


@pytest.mark.parametrize("cash", [5, -5, (1 << 62) - 1])
def test_exact_rounding_uses_wide_intermediates(cash: int) -> None:
    parameters = Parameters(5000, 0, 0)
    scalar = ScalarPolicy(parameters)
    batch = BatchPolicy(parameters, 1)
    observations = Observations(
        np.array([0], dtype=np.int64),
        np.array([0], dtype=np.int64),
        np.array([cash], dtype=object),
        np.array([0], dtype=object),
        np.array([1_000_000_000], dtype=object),
    )
    expected = (abs(cash) + 1) // 2 * (-1 if cash < 0 else 1)
    assert scalar(Observation(0, cash, 0, 1_000_000_000)) == expected
    assert batch(observations) == [expected]


def test_overflow_is_not_silently_wrapped_in_batch_arithmetic() -> None:
    parameters = Parameters(400, 1000, 500)
    with pytest.raises(OverflowError):
        ScalarPolicy(parameters)(Observation(0, (1 << 63) - 1, 1, 1))
    with pytest.raises(OverflowError):
        BatchPolicy(parameters, 1)(
            Observations(
                np.array([0], dtype=np.int64),
                np.array([0], dtype=np.int64),
                np.array([(1 << 63) - 1], dtype=object),
                np.array([1], dtype=object),
                np.array([1], dtype=object),
            )
        )


def test_post_cashflow_review_and_ordered_claim_prefix_are_explicit() -> None:
    case = Case(
        scenario(
            checking(("retiree", Decimal(100)), ("world", Decimal(0))),
            horizon_months=1,
            tax_profiles=[],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=0,
                    cause_id="current-income",
                    from_agent_id="world",
                    from_account_id="checking",
                    to_agent_id="retiree",
                    to_account_id="checking",
                    amount=Decimal(100),
                )
            ],
            scheduled_obligations=[
                ScheduledObligation(
                    month=0,
                    obligation_id="due-bill",
                    obligation_type=ObligationType.OUTSIDE_RENT,
                    agent_id="retiree",
                    from_account_id="checking",
                    to_agent_id="world",
                    to_account_id="checking",
                    amount_due=Decimal(30),
                )
            ],
        ),
        rollout_count=1,
        series={InflationKey(): np.ones((1, 2))},
    )
    result = run(
        json.dumps(case.compiled_run.execution_input), SpendingPolicy(BatchPolicy(Parameters(10_000, 0, 0), 1), {}), [0]
    )
    [row] = result.rollouts
    assert consumption(result) == ([[20_000]], [[0]])  # includes the current $100 contribution
    assert row.stop == RejectedAction(month=0, action_index=1)
    assert row.summary.cash[0].values == [10_000, 17_000]  # the earlier bill stays paid
    assert isinstance(row.summary.payments[0].receipt.outcome, Paid)
    assert row.summary.unpaid_claims == []


def test_current_cpi_is_routed_without_future_values() -> None:
    case = Case(
        scenario(checking(("retiree", Decimal(1))), horizon_months=2, tax_profiles=[]),
        rollout_count=2,
        series={InflationKey(): np.array([[2.0, 3.0, 90.0], [4.0, 5.0, 70.0]])},
    )
    session = ActionSession(json.dumps(case.compiled_run.execution_input), "retiree", [1, 0], capture="summary")
    batch = session.start()
    assert not isinstance(batch, Finished)
    assert [row.observation.cpi for row in batch] == [(4_000_000_000, 4_000_000_000), (2_000_000_000, 2_000_000_000)]
    batch = session.advance([DecisionActions(row.rollout_id, 0, []) for row in batch])
    assert not isinstance(batch, Finished)
    assert [row.observation.cpi for row in batch] == [(5_000_000_000, 4_000_000_000), (3_000_000_000, 2_000_000_000)]
    session.close()


def test_cpi_dependent_rule_does_not_invent_a_flat_missing_index() -> None:
    case = Case(
        scenario(checking(("retiree", Decimal(100)), ("world", Decimal(0))), horizon_months=1, tax_profiles=[]),
        rollout_count=1,
    )
    with pytest.raises(ValueError, match="requires a supplied CPI"):
        run(
            json.dumps(case.compiled_run.execution_input),
            SpendingPolicy(BatchPolicy(Parameters(400, 0, 0), 1), {}),
            [0],
        )


def test_authored_funding_pays_canonical_tax_claims_and_replays_compactly() -> None:
    stock = SecurityKey(symbol="synthetic-tax-stock")
    rules = Jurisdiction(
        jurisdiction_id="synthetic-flat-tax",
        level=JurisdictionLevel.FEDERAL,
        ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
        ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
        standard_deduction={FilingStatus.SINGLE: Decimal(0)},
        max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    )
    prepared = compile_run(
        scenario(
            checking(("retiree", Decimal(0)), ("world", Decimal(0))),
            horizon_months=13,
            initial_lots=[
                InitialLot(
                    lot_id="tax-lot",
                    agent_id="retiree",
                    account_id="brokerage",
                    asset=stock,
                    purchase_month_index=-24,
                    quantity=10,
                    cost_basis_per_unit=Decimal(40),
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id="retiree",
                    jurisdiction_ids=[rules.jurisdiction_id],
                    tax_authority_agent_id="world",
                    prior_year_tax=Decimal(0),
                )
            ],
        ),
        rollout_count=1,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(stock, np.full((1, 14), 100.0)), (InflationKey(), np.ones((1, 14)))], rollout_count=1, horizon_months=13
        ),
        jurisdictions={rules.jurisdiction_id: rules},
        locations={},
    )
    outputs = [
        run(
            json.dumps(prepared.execution_input),
            SpendingPolicy(BatchPolicy(Parameters(400, 0, 0), 1), {("brokerage", str(stock.symbol)): 1}),
            [0],
            capture="forensic" if forensic else "summary",
        ).rollouts[0]
        for forensic in [False, True]
    ]
    assert outputs[0].summary == outputs[1].summary
    summary = outputs[0].summary
    taxes = [payment for payment in summary.payments if payment.target is not None and payment.target.is_tax_payment]
    assert [(payment.month, payment.receipt.amount_requested, payment.receipt.outcome) for payment in taxes] == [
        (12, 240, Paid())
    ]
    assert summary.tax_accruals
    assert summary.tax_payments
    assert summary.public_holdings[0].values[-1] == 91_760


def test_actual_profile_cli_uses_same_batch_session_for_both_authoring_forms(tmp_path: Path) -> None:
    reports = []
    for authoring in ["scalar", "batch"]:
        directory = tmp_path / authoring
        subprocess.run(
            [
                get_required_path("_main/finance/augur/x/bounded_spending/profile_bin"),
                "--rollouts",
                "3",
                "--horizon-months",
                "36",
                "--native-threads",
                "1",
                "--authoring",
                authoring,
                "--capture",
                "summary",
                "--output-dir",
                str(directory),
            ],
            check=True,
        )
        reports.append(json.loads((directory / "report.json").read_text()))
        assert (directory / "execution.prof").stat().st_size > 0
    assert reports[0]["input_sha256"] == reports[1]["input_sha256"]
    assert reports[0]["output_sha256"] == reports[1]["output_sha256"]
    assert reports[0]["observed_path_months"] == 108


if __name__ == "__main__":
    pytest_bazel.main()
