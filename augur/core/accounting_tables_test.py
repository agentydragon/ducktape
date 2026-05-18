"""Round-trip and aggregation tests for the columnar accounting trace."""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.core.accounting import AccountingCauseType, ChartAccountRole, JournalEntryType, PostingSide
from augur.core.accounting_tables import AccountingTrace, AccountingTraceBuilder, validate_trace
from augur.core.policy_runtime import BalanceSnapshotBatch, JournalEntryBatch, PostingBatch


def _build_simple_trace() -> AccountingTrace:
    """Two journal entries (mortgage payment + opening balance) across a
    (2 rollouts × 3 months) grid. Each posting amount is a `(rollouts,
    months)` numpy array; the builder fans them out into one fact-table
    row per active cell.
    """
    builder = AccountingTraceBuilder()

    opening = np.array([[1000.0, 0.0, 0.0], [1000.0, 0.0, 0.0]])
    builder.record_entry(
        month_index=np.array([0, 1, 2], dtype=np.int64),
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.OPENING_BALANCE,
            cause_type=AccountingCauseType.OPENING_BALANCE,
            cause_id_prefix="opening:cash",
            actor_id="me",
            description="opening cash",
            postings=(
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH, side=PostingSide.DEBIT, amount_usd=opening, actor_id="me"
                ),
                PostingBatch(
                    role=ChartAccountRole.OPENING_EQUITY, side=PostingSide.CREDIT, amount_usd=opening, actor_id="me"
                ),
            ),
        ),
    )

    mortgage_payment = np.array([[0.0, 500.0, 500.0], [0.0, 600.0, 600.0]])
    mortgage_principal = np.array([[0.0, 100.0, 100.0], [0.0, 150.0, 150.0]])
    mortgage_interest = mortgage_payment - mortgage_principal
    builder.record_entry(
        month_index=np.array([0, 1, 2], dtype=np.int64),
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT,
            cause_type=AccountingCauseType.SCHEDULED_EVENT,
            cause_id_prefix="mortgage:payment",
            actor_id="me",
            description="monthly mortgage payment",
            postings=(
                PostingBatch(
                    role=ChartAccountRole.MORTGAGE_PAYABLE,
                    side=PostingSide.DEBIT,
                    amount_usd=mortgage_principal,
                    actor_id="me",
                ),
                PostingBatch(
                    role=ChartAccountRole.MORTGAGE_INTEREST_EXPENSE,
                    side=PostingSide.DEBIT,
                    amount_usd=mortgage_interest,
                    actor_id="me",
                ),
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH,
                    side=PostingSide.CREDIT,
                    amount_usd=mortgage_payment,
                    actor_id="me",
                ),
            ),
        ),
    )

    balance = np.array([[1000.0, 900.0, 800.0], [1000.0, 850.0, 700.0]])
    builder.record_snapshot(
        month_index=np.array([0, 1, 2], dtype=np.int64),
        snapshot=BalanceSnapshotBatch(role=ChartAccountRole.CHECKING_CASH, amount_usd=balance, actor_id="me"),
    )

    return builder.finalize()


def test_round_trip_materializes_to_pydantic() -> None:
    trace = _build_simple_trace()

    chart_accounts = trace.chart_accounts_tuple()
    # opening: CHECKING_CASH + OPENING_EQUITY; mortgage: MORTGAGE_PAYABLE +
    # MORTGAGE_INTEREST_EXPENSE + CHECKING_CASH (CHECKING_CASH dedups).
    account_ids = {a.chart_account_id for a in chart_accounts}
    assert len(account_ids) == 4

    # Opening fires every (rollout, month) where amount > 0 — only month 0.
    # Mortgage fires month 1+2 across 2 rollouts. So 2 + 4 = 6 JEs.
    journal_entries = trace.journal_entries_tuple()
    assert len(journal_entries) == 6

    # Each opening entry has 2 postings, each mortgage entry has 3 postings.
    # So 2 × 2 + 4 × 3 = 16 postings.
    postings = trace.postings_tuple()
    assert len(postings) == 16

    # Sum debits == sum credits for each entry.
    by_entry: dict[str, list] = {}
    for posting in postings:
        by_entry.setdefault(posting.journal_entry_id, []).append(posting)
    for entry_postings in by_entry.values():
        debits = sum(p.amount_usd for p in entry_postings if p.side is PostingSide.DEBIT)
        credits = sum(p.amount_usd for p in entry_postings if p.side is PostingSide.CREDIT)
        assert abs(debits - credits) < 0.005


def test_validate_trace_accepts_balanced() -> None:
    trace = _build_simple_trace()
    validate_trace(trace)


def test_filter_postings_by_role() -> None:
    trace = _build_simple_trace()
    cash_debits = trace.filter_postings(role=ChartAccountRole.CHECKING_CASH, side=PostingSide.DEBIT)
    # 2 opening entries (1 per rollout) × 1 debit = 2.
    assert len(cash_debits) == 2

    cash_credits = trace.filter_postings(role=ChartAccountRole.CHECKING_CASH, side=PostingSide.CREDIT)
    # 4 mortgage entries × 1 credit = 4.
    assert len(cash_credits) == 4


def test_posting_amount_matrix_matches_per_role_aggregation() -> None:
    trace = _build_simple_trace()
    month_index = np.array([0, 1, 2], dtype=np.int64)

    cash_debits_matrix = trace.posting_amount_matrix(
        rollout_count=2, month_index=month_index, role=ChartAccountRole.CHECKING_CASH, side=PostingSide.DEBIT
    )
    np.testing.assert_allclose(cash_debits_matrix, [[1000.0, 0.0, 0.0], [1000.0, 0.0, 0.0]])

    cash_credits_matrix = trace.posting_amount_matrix(
        rollout_count=2, month_index=month_index, role=ChartAccountRole.CHECKING_CASH, side=PostingSide.CREDIT
    )
    np.testing.assert_allclose(cash_credits_matrix, [[0.0, 500.0, 500.0], [0.0, 600.0, 600.0]])


def test_balance_snapshot_amount_matrix() -> None:
    trace = _build_simple_trace()
    month_index = np.array([0, 1, 2], dtype=np.int64)

    matrix = trace.balance_snapshot_amount_matrix(
        rollout_count=2, month_index=month_index, role=ChartAccountRole.CHECKING_CASH
    )
    np.testing.assert_allclose(matrix, [[1000.0, 900.0, 800.0], [1000.0, 850.0, 700.0]])


def test_filter_journal_entries_by_type_and_rollout() -> None:
    trace = _build_simple_trace()

    mortgage_entries = trace.filter_journal_entries(journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT)
    assert len(mortgage_entries) == 4

    rollout_0_entries = trace.filter_journal_entries(journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT, rollout=0)
    assert len(rollout_0_entries) == 2
    assert all(entry.rollout_index == 0 for entry in rollout_0_entries)


def test_with_trajectory_identity_propagates_to_postings() -> None:
    trace = _build_simple_trace()
    identity = {
        0: {
            "path_set_id": "path_set_a",
            "exogenous_path_id": "rollout_0_path",
            "scenario_input_id": "scenario_a",
            "projection_trajectory_id": "trajectory_a_0",
        },
        1: {
            "path_set_id": "path_set_a",
            "exogenous_path_id": "rollout_1_path",
            "scenario_input_id": "scenario_a",
            "projection_trajectory_id": "trajectory_a_1",
        },
    }
    enriched = trace.with_trajectory_identity(identity)
    for posting in enriched.postings_tuple():
        assert posting.path_set_id == "path_set_a"
        assert posting.exogenous_path_id == f"rollout_{posting.rollout_index}_path"
        assert posting.scenario_input_id == "scenario_a"
        assert posting.projection_trajectory_id == f"trajectory_a_{posting.rollout_index}"


def test_sorted_canonical_orders_by_month_then_rollout() -> None:
    trace = _build_simple_trace().sorted_canonical()
    months = trace.postings.column("month_index").to_pylist()
    rollouts = trace.postings.column("rollout_index").to_pylist()
    # Each posting row should sort by (month, rollout) primarily.
    pairs = list(zip(months, rollouts, strict=True))
    assert pairs == sorted(pairs)


if __name__ == "__main__":
    pytest_bazel.main()
