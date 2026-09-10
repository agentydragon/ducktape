"""Run the documented CLI and inspect canonical action, lot, tax and stopping results."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from finance.augur.x.monthly_actions.run import run_example
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture(scope="module")
def example(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    output = tmp_path_factory.mktemp("monthly-actions") / "results"
    subprocess.run(
        [get_required_path(own_repo_rlocation("finance/augur/x/monthly_actions/run_bin")), "--output-dir", output],
        check=True,
    )
    document = json.loads((output / "outcomes.json").read_text())
    if not isinstance(document, dict):
        raise ValueError("example CLI output must be a JSON object")
    return document


def test_bill_and_later_tax_are_paid_from_actual_sale_proceeds(example: dict[str, Any]) -> None:
    funded = example["rollouts"][0]
    financial = funded["trace"]["financial"]
    assert funded["stop"] is None
    assert financial["failed_month"] is None
    assert [(row["month"], row["action_index"]) for row in funded["trace"]["receipts"]] == [(0, 0), (0, 1), (12, 0)]
    assert [next(iter(row["action"])) for row in funded["trace"]["receipts"]] == ["Sell", "PayClaim", "PayClaim"]
    assert all(row["outcome"] == "Executed" for row in funded["trace"]["receipts"])
    sale = financial["dispositions"][0]
    assert (sale["proceeds"], sale["basis"], sale["realized_gain"]) == (20_000, 8_000, 12_000)
    assert [(row["month"], row["amount_paid"]) for row in financial["obligations"]] == [(0, 15_000), (12, 1_200)]
    assert [(row["month"], row["amount_paid"]) for row in financial["tax_payments"]] == [(12, 1_200)]
    assert [(row["month"], row["long_term_gain"], row["total_tax"]) for row in financial["tax_accruals"]] == [
        (11, 12_000, 1_200)
    ]
    assert len(financial["months"]) == 14
    closing = financial["months"][-1]
    assert [
        row["balance"]
        for row in closing["balances"]
        if row["account"] == {"agent_id": "example-household", "account_id": "checking"}
    ] == [3_800]
    assert [(lot["units_remaining"], lot["basis_remaining"]) for lot in closing["lots"]] == [(0, 0)]
    for entry in financial["journal"]:
        assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_unfunded_bill_preserves_sale_and_stops_without_later_actions(example: dict[str, Any]) -> None:
    stopped = example["rollouts"][1]
    financial = stopped["trace"]["financial"]
    assert stopped["stop"] == {"RejectedAction": {"month": 0, "action_index": 1}}
    assert financial["failed_month"] == 0
    assert len(stopped["trace"]["receipts"]) == 2
    assert stopped["trace"]["receipts"][0]["outcome"] == "Executed"
    assert stopped["trace"]["receipts"][1]["outcome"] == {
        "Rejected": {"Payment": {"InsufficientCash": {"available": 10_000}}}
    }
    assert [(row["proceeds"], row["basis"]) for row in financial["dispositions"]] == [(10_000, 8_000)]
    assert all(row["amount_paid"] == 0 for row in financial["obligations"])
    assert financial["tax_payments"] == []
    assert len(financial["months"]) == 2
    stopped_book = financial["months"][-1]
    assert [
        row["balance"]
        for row in stopped_book["balances"]
        if row["account"] == {"agent_id": "example-household", "account_id": "checking"}
    ] == [10_000]
    assert [(lot["units_remaining"], lot["basis_remaining"]) for lot in stopped_book["lots"]] == [(0, 0)]


def test_original_path_identity_survives_reordering_and_selected_replay(
    example: dict[str, Any], tmp_path: Path
) -> None:
    expected = {row["trace"]["financial"]["rollout_id"]: row for row in example["rollouts"]}
    for name, ids in (("reordered", (1, 0)), ("selected", (1,))):
        actual = run_example(tmp_path / name, ids)["rollouts"]
        assert [row["trace"]["financial"]["rollout_id"] for row in actual] == list(ids)
        assert actual == [expected[rollout_id] for rollout_id in ids]


@pytest.mark.parametrize("capture", ["summary", "dense"])
def test_actual_cli_capture_agrees_with_selected_forensic_results(
    example: dict[str, Any], tmp_path: Path, capture: str
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
    document = json.loads((output / "outcomes.json").read_text())
    expected = {row["rollout_id"]: row for row in example["rollouts"]}
    assert [row["rollout_id"] for row in document["rollouts"]] == [1, 0]
    for result in document["rollouts"]:
        detailed = expected[result["rollout_id"]]
        summary = result["summary"]
        assert summary == detailed["summary"]
        assert result["stop"] == detailed["stop"]
        assert summary["ending_book"] == detailed["trace"]["financial"]["months"][-1]
        assert len(summary["cash"][0]["values"]) == summary["ending_book"]["month"] + 1
        for field in ("tax_accruals", "tax_payments", "tax_settlements"):
            assert summary[field] == detailed["trace"]["financial"][field]
        if capture == "summary":
            assert result["trace"] is None
        else:
            assert result["trace"]["financial"]["journal"] == []
    funded = document["rollouts"][1]["summary"]
    assert funded["payments"][1]["receipt"]["amount_requested"] == 1_200
    assert funded["payments"][1]["target"]["is_tax_payment"]
    assert funded["cash"][0]["values"][-1] == 3_800
    stopped = document["rollouts"][0]["summary"]
    assert stopped["ending_mark_month"] == 0
    assert stopped["payments"][0]["receipt"]["outcome"] == {"Rejected": {"InsufficientCash": {"available": 10_000}}}
    assert stopped["last_receipts"][0]["outcome"] == "Executed"
    assert stopped["unpaid_claims"][0]["amount_due"] == 15_000


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
    prepared = json.loads((output_dir / "execution-input.json").read_text())["scenario"]
    assert prepared["initial_lots"] == []
    assert prepared["target_allocation_policies"] == []
    assert [(pool["agent_id"], pool["account_id"], pool["asset_id"]) for pool in prepared["holding_pools"]] == [
        ("example-household", "brokerage", "example-stock")
    ]
    rollouts = json.loads((output_dir / "outcomes.json").read_text())["rollouts"]
    for rollout, units in zip(rollouts, [2_000_000, 4_000_000], strict=True):
        assert rollout["stop"] is None
        assert [next(iter(row["action"])) for row in rollout["trace"]["receipts"]] == [
            "Buy",
            "Sell",
            "PayClaim",
            "PayClaim",
        ]
        financial = rollout["trace"]["financial"]
        assert financial["months"][0]["lots"] == []
        bought = financial["months"][1]["lots"][0]
        assert (bought["account_id"], bought["units_remaining"], bought["basis_remaining"]) == (
            "brokerage",
            units,
            20_000,
        )
        sale = financial["dispositions"][0]
        assert (sale["proceeds"], sale["basis"], sale["realized_gain"]) == (24_000, 20_000, 4_000)
        assert [(row["month"], row["amount_paid"]) for row in financial["obligations"]] == [(1, 15_000), (12, 800)]
        assert financial["tax_accruals"][0]["short_term_gain"] == 4_000
        ending_cash = next(
            row["balance"]
            for row in financial["months"][-1]["balances"]
            if row["account"] == {"agent_id": "example-household", "account_id": "checking"}
        )
        assert ending_cash == 8_200

    compact = run_example(tmp_path / "cash-only-compact", capture="summary", cash_only_start=True)["rollouts"]
    for summary, detailed in zip(compact, rollouts, strict=True):
        assert summary["summary"] == detailed["summary"]
        holding = summary["summary"]["public_holdings"][0]
        assert holding["account"] == {"agent_id": "example-household", "account_id": "brokerage"}
        assert holding["values"][:3] == [0, 24_000, 0]


if __name__ == "__main__":
    pytest_bazel.main()
