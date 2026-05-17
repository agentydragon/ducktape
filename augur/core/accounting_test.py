from __future__ import annotations

import pytest
import pytest_bazel

from augur.core.accounting import (
    AccountingCause,
    AccountingCauseType,
    AccountingValidationError,
    ChartAccount,
    ChartAccountRole,
    ChartAccountType,
    JournalEntry,
    JournalEntryType,
    Posting,
    PostingSide,
    chart_account_id,
    validate_accounting_trace,
)


def test_validate_accounting_trace_accepts_balanced_journal() -> None:
    cash_id = chart_account_id(ChartAccountRole.CHECKING_CASH, actor_id="owner", source_account_id="checking")
    expense_id = chart_account_id(ChartAccountRole.MONTHLY_LIVING_EXPENSE, actor_id="owner")

    validate_accounting_trace(
        chart_accounts=(
            ChartAccount(
                chart_account_id=cash_id,
                account_type=ChartAccountType.ASSET,
                role=ChartAccountRole.CHECKING_CASH,
                actor_id="owner",
                source_account_id="checking",
            ),
            ChartAccount(
                chart_account_id=expense_id,
                account_type=ChartAccountType.EXPENSE,
                role=ChartAccountRole.MONTHLY_LIVING_EXPENSE,
                actor_id="owner",
            ),
        ),
        journal_entries=(
            JournalEntry(
                journal_entry_id="monthly_spend:rollout:0:month:1",
                rollout_index=0,
                month_index=1,
                journal_entry_type=JournalEntryType.CASH_EXPENSE,
                actor_id="owner",
                cause=AccountingCause(
                    cause_type=AccountingCauseType.POLICY_DECISION,
                    cause_id="policy:spend:rollout:0:month:1",
                    policy_id="spend",
                ),
            ),
        ),
        postings=(
            Posting(
                posting_id="monthly_spend:rollout:0:month:1:debit",
                journal_entry_id="monthly_spend:rollout:0:month:1",
                rollout_index=0,
                month_index=1,
                chart_account_id=expense_id,
                side=PostingSide.DEBIT,
                amount_usd=100,
            ),
            Posting(
                posting_id="monthly_spend:rollout:0:month:1:credit",
                journal_entry_id="monthly_spend:rollout:0:month:1",
                rollout_index=0,
                month_index=1,
                chart_account_id=cash_id,
                side=PostingSide.CREDIT,
                amount_usd=100,
            ),
        ),
    )


def test_validate_accounting_trace_rejects_unbalanced_journal() -> None:
    cash_id = chart_account_id(ChartAccountRole.CHECKING_CASH, actor_id="owner")
    expense_id = chart_account_id(ChartAccountRole.MONTHLY_LIVING_EXPENSE, actor_id="owner")

    with pytest.raises(AccountingValidationError, match="unbalanced"):
        validate_accounting_trace(
            chart_accounts=(
                ChartAccount(
                    chart_account_id=cash_id,
                    account_type=ChartAccountType.ASSET,
                    role=ChartAccountRole.CHECKING_CASH,
                    actor_id="owner",
                ),
                ChartAccount(
                    chart_account_id=expense_id,
                    account_type=ChartAccountType.EXPENSE,
                    role=ChartAccountRole.MONTHLY_LIVING_EXPENSE,
                    actor_id="owner",
                ),
            ),
            journal_entries=(
                JournalEntry(
                    journal_entry_id="broken",
                    rollout_index=0,
                    month_index=1,
                    journal_entry_type=JournalEntryType.CASH_EXPENSE,
                    actor_id="owner",
                    cause=AccountingCause(
                        cause_type=AccountingCauseType.POLICY_DECISION, cause_id="policy:spend:rollout:0:month:1"
                    ),
                ),
            ),
            postings=(
                Posting(
                    posting_id="broken:debit",
                    journal_entry_id="broken",
                    rollout_index=0,
                    month_index=1,
                    chart_account_id=expense_id,
                    side=PostingSide.DEBIT,
                    amount_usd=100,
                ),
                Posting(
                    posting_id="broken:credit",
                    journal_entry_id="broken",
                    rollout_index=0,
                    month_index=1,
                    chart_account_id=cash_id,
                    side=PostingSide.CREDIT,
                    amount_usd=80,
                ),
            ),
        )


def test_validate_accounting_trace_rejects_unknown_account() -> None:
    with pytest.raises(AccountingValidationError, match="unknown chart account"):
        validate_accounting_trace(
            chart_accounts=(),
            journal_entries=(
                JournalEntry(
                    journal_entry_id="entry",
                    rollout_index=0,
                    month_index=0,
                    journal_entry_type=JournalEntryType.OPENING_BALANCE,
                    actor_id="owner",
                    cause=AccountingCause(cause_type=AccountingCauseType.OPENING_BALANCE, cause_id="opening:owner"),
                ),
            ),
            postings=(
                Posting(
                    posting_id="entry:posting",
                    journal_entry_id="entry",
                    rollout_index=0,
                    month_index=0,
                    chart_account_id="missing",
                    side=PostingSide.DEBIT,
                    amount_usd=1,
                ),
            ),
        )


if __name__ == "__main__":
    pytest_bazel.main()
