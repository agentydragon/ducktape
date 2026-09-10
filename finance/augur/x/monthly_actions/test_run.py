"""Run the documented CLI and inspect canonical action, lot, tax and stopping results."""

import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.rust.invocation import read_prepared_input
from finance.augur.sim.books import AccountRef
from finance.augur.sim.results import (
    Executed,
    Finished,
    InsufficientCash,
    PaymentRejected,
    PaymentRejection,
    Rejected,
    RejectedAction,
)
from finance.augur.x.monthly_actions.run import run_example
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture(scope="module")
def example(tmp_path_factory: pytest.TempPathFactory) -> Finished:
    output = tmp_path_factory.mktemp("monthly-actions") / "results"
    subprocess.run(
        [get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/run_bin")), "--output-dir", output],
        check=True,
    )
    return Finished.model_validate_json((output / "outcomes.json").read_text())


def test_bill_and_later_tax_are_paid_from_actual_sale_proceeds(example: Finished) -> None:
    funded = example.rollouts[0]
    financial = funded.trace
    assert financial is not None
    assert funded.stop is None
    assert [(row.month, row.action_index) for row in financial.receipts] == [(0, 0), (0, 1), (12, 0)]
    assert [row.action.kind for row in financial.receipts] == ["Sell", "PayClaim", "PayClaim"]
    assert all(isinstance(row.outcome, Executed) for row in financial.receipts)
    assert financial.events.lot_dispositions.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (
        20_000,
        8_000,
    )
    assert financial.events.lot_dispositions.get_column("realized_gain_quanta").to_list() == [12_000]
    assert [(row.month, row.receipt.amount_paid) for row in funded.summary.payments] == [(0, 15_000), (12, 1_200)]
    assert [(row.month, row.amount_paid) for row in funded.summary.tax_payments] == [(12, 1_200)]
    assert [(row.month, row.long_term_gain, row.total_tax) for row in funded.summary.tax_accruals] == [
        (11, 12_000, 1_200)
    ]
    assert len(financial.books) == 14
    closing = financial.books[-1]
    assert [
        row.balance
        for row in closing.balances
        if row.account == AccountRef(agent_id="example-household", account_id="checking")
    ] == [3_800]
    assert [(lot.units_remaining, lot.basis_remaining) for lot in closing.lots] == [(0, 0)]
    for entry in financial.journal:
        assert sum(posting.amount for posting in entry.postings) == 0


def test_unfunded_bill_preserves_sale_and_stops_without_later_actions(example: Finished) -> None:
    stopped = example.rollouts[1]
    financial = stopped.trace
    assert financial is not None
    assert stopped.stop == RejectedAction(month=0, action_index=1)
    assert len(financial.receipts) == 2
    assert financial.receipts[0].outcome == Executed()
    assert financial.receipts[1].outcome == Rejected(reason=PaymentRejection(detail=InsufficientCash(available=10_000)))
    assert financial.events.lot_dispositions.select("proceeds_quanta", "cost_basis_consumed_quanta").rows() == [
        (10_000, 8_000)
    ]
    assert all(row.receipt.amount_paid == 0 for row in stopped.summary.payments)
    assert stopped.summary.tax_payments == []
    assert len(financial.books) == 2
    stopped_book = financial.books[-1]
    assert [
        row.balance
        for row in stopped_book.balances
        if row.account == AccountRef(agent_id="example-household", account_id="checking")
    ] == [10_000]
    assert [(lot.units_remaining, lot.basis_remaining) for lot in stopped_book.lots] == [(0, 0)]


def test_original_path_identity_survives_reordering_and_selected_replay(example: Finished, tmp_path: Path) -> None:
    expected = {row.rollout_id: row for row in example.rollouts}
    for name, ids in (("reordered", (1, 0)), ("selected", (1,))):
        actual = run_example(tmp_path / name, ids).rollouts
        assert [row.rollout_id for row in actual] == list(ids)
        assert actual == [expected[rollout_id] for rollout_id in ids]


@pytest.mark.parametrize("capture", ["summary", "dense"])
def test_actual_cli_capture_agrees_with_selected_forensic_results(
    example: Finished, tmp_path: Path, capture: str
) -> None:
    output = tmp_path / capture
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/run_bin")),
            "--output-dir",
            output,
            "--capture",
            capture,
            "--rollout",
            "1",
            "--rollout",
            "0",
        ],
        check=True,
    )
    document = Finished.model_validate_json((output / "outcomes.json").read_text())
    expected = {row.rollout_id: row for row in example.rollouts}
    assert [row.rollout_id for row in document.rollouts] == [1, 0]
    for result in document.rollouts:
        detailed = expected[result.rollout_id]
        summary = result.summary
        assert summary == detailed.summary
        assert result.stop == detailed.stop
        assert detailed.trace is not None
        assert summary.ending_book == detailed.trace.books[-1]
        assert len(summary.cash[0].values) == summary.ending_book.month + 1
        if capture == "summary":
            assert result.trace is None
        else:
            assert result.trace is not None
            assert result.trace.journal == []
    funded = document.rollouts[1].summary
    assert funded.payments[1].receipt.amount_requested == 1_200
    assert funded.payments[1].target is not None
    assert funded.payments[1].target.is_tax_payment
    assert funded.cash[0].values[-1] == 3_800
    stopped = document.rollouts[0].summary
    assert stopped.ending_mark_month == 0
    assert stopped.payments[0].receipt.outcome == PaymentRejected(reason=InsufficientCash(available=10_000))
    assert stopped.last_receipts[0].outcome == Executed()
    assert stopped.unpaid_claims[0].amount_due == 15_000


def test_cash_only_cli_buys_unheld_asset_then_sells_and_pays_tax(tmp_path: Path) -> None:
    output_dir = tmp_path / "cash-only"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/run_bin")),
            "--output-dir",
            output_dir,
            "--cash-only-start",
        ],
        check=True,
    )
    prepared = read_prepared_input(output_dir / "execution-input.json").scenario
    assert not prepared.initial_lots
    assert [(pool.agent_id, pool.account_id, pool.asset_id) for pool in prepared.holding_pools] == [
        ("example-household", "brokerage", "example-stock")
    ]
    rollouts = Finished.model_validate_json((output_dir / "outcomes.json").read_text()).rollouts
    for rollout, units in zip(rollouts, [2_000_000, 4_000_000], strict=True):
        assert rollout.stop is None
        assert rollout.trace is not None
        assert [row.action.kind for row in rollout.trace.receipts] == ["Buy", "Sell", "PayClaim", "PayClaim"]
        financial = rollout.trace
        assert financial.books[0].lots == []
        bought = financial.books[1].lots[0]
        assert (bought.account_id, bought.units_remaining, bought.basis_remaining) == ("brokerage", units, 20_000)
        assert financial.events.lot_dispositions.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (
            24_000,
            20_000,
        )
        assert financial.events.lot_dispositions.get_column("realized_gain_quanta").to_list() == [4_000]
        assert [(row.month, row.receipt.amount_paid) for row in rollout.summary.payments] == [(1, 15_000), (12, 800)]
        assert rollout.summary.tax_accruals[0].short_term_gain == 4_000
        ending_cash = next(
            row.balance
            for row in financial.books[-1].balances
            if row.account == AccountRef(agent_id="example-household", account_id="checking")
        )
        assert ending_cash == 8_200

    compact = run_example(tmp_path / "cash-only-compact", capture="summary", cash_only_start=True).rollouts
    for summary, detailed in zip(compact, rollouts, strict=True):
        assert summary.summary == detailed.summary
        holding = summary.summary.public_holdings[0]
        assert holding.account == AccountRef(agent_id="example-household", account_id="brokerage")
        assert holding.values[:3] == [0, 24_000, 0]


def test_profile_entrypoint_compares_matching_capture_workloads(tmp_path: Path) -> None:
    reports = []
    for capture in ("summary", "forensic"):
        output = tmp_path / capture
        subprocess.run(
            [
                get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/profile_bin")),
                "--output-dir",
                output,
                "--rollouts",
                "4",
                "--horizon-months",
                "13",
                "--native-threads",
                "1",
                "--capture",
                capture,
            ],
            check=True,
        )
        reports.append(json.loads((output / "report.json").read_text()))
        assert (output / "execution.prof").stat().st_size > 0
    compact, detailed = reports
    assert compact["input_sha256"] == detailed["input_sha256"]
    assert compact["compact_sha256"] == detailed["compact_sha256"]
    assert compact["observed_path_months"] == detailed["observed_path_months"] == 28
    assert compact["output_bytes"] < detailed["output_bytes"]


def test_population_cli_keeps_original_ids_for_selected_replay(tmp_path: Path) -> None:
    output = tmp_path / "population"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/run_bin")),
            "--output-dir",
            output,
            "--rollouts",
            "6",
            "--horizon-months",
            "24",
            "--capture",
            "summary",
        ],
        check=True,
    )
    population = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    replay = run_example(tmp_path / "replay", (5, 2), rollout_count=6, horizon_months=24).rollouts
    assert [row.rollout_id for row in population] == list(range(6))
    for result in replay:
        original = population[result.rollout_id]
        assert result.summary == original.summary
        assert result.stop == original.stop


if __name__ == "__main__":
    pytest_bazel.main()
