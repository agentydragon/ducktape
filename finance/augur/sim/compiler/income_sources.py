"""Stable reporting order and execution-input labels for typed income categories."""

from finance.augur.sim.scenario import InterestIncome, TransferIncomeCategory


def income_source_sort_key(category: TransferIncomeCategory) -> tuple[int, str]:
    """Ordinary first, then interest by issuer, corporate last."""

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return (1, issuer) if issuer is not None else (2, "")
    return (0, "")


def income_source_wire_id(category: TransferIncomeCategory) -> str:
    """The financial income ledger's label for one prepared category."""

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return f"interest:{issuer if issuer is not None else 'corporate'}"
    return "ordinary"
