"""Stable reporting order and execution-input labels for typed income categories."""

from finance.augur.sim.scenario import InterestIncome, QualifiedDividendIncome, TransferIncomeCategory


def income_source_sort_key(category: TransferIncomeCategory) -> tuple[int, str]:
    """Ordinary first, then qualified dividends, then interest by issuer, corporate last."""

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return (2, issuer) if issuer is not None else (3, "")
    if isinstance(category, QualifiedDividendIncome):
        return (1, "")
    return (0, "")


def income_source_wire_id(category: TransferIncomeCategory) -> str:
    """The financial income ledger's label for one prepared category."""

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return f"interest:{issuer if issuer is not None else 'corporate'}"
    if isinstance(category, QualifiedDividendIncome):
        return "qualified_dividend"
    return "ordinary"
