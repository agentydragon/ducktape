from datetime import datetime
from itertools import product
from types import SimpleNamespace

import pytest
import pytest_bazel

from x.eob_matching import matcher
from x.eob_matching.models import EOB, BankPayment, MatchConfidence, Matched, NotMatched, NotMatchedReason

PAYMENT_DATE = datetime(2001, 1, 2)


def _eob(cents: int, *claims: str, processed: datetime | None = None) -> EOB:
    return EOB(
        statement_date="2001-01-01",
        service_dates="",
        claim_nums=list(claims),
        provider="Synthetic provider",
        your_costs=[],
        plan_paid_cents=cents,
        plan_paid_total=cents / 100,
        latest_proc_date=processed,
    )


def _payment(cents: int, payment_id: str) -> BankPayment:
    return BankPayment(date="01/02/2001", date_dt=PAYMENT_DATE, payment_id=payment_id, amount=cents / 100, cents=cents)


def test_subset_search_agrees_with_brute_force() -> None:
    for size in range(6):
        for amounts in product(range(4), repeat=size):
            items = [_eob(cents, str(i)) for i, cents in enumerate(amounts)]
            subsets = [
                [item for take, item in zip(selection, items, strict=True) if take]
                for selection in product((False, True), repeat=size)
            ]
            for target in range(1, 10):
                solutions = [
                    subset
                    for subset in subsets
                    if all(item.plan_paid_cents > 0 for item in subset)
                    and sum(item.plan_paid_cents for item in subset) == target
                ]
                witness = matcher.dp_find_solution(items, target)
                if solutions:
                    assert witness is not None
                    assert witness[::-1] in solutions
                else:
                    assert witness is None
                assert matcher.dp_count_solutions(items, target, cap=2) == min(len(solutions), 3)
                assert matcher.dp_count_solutions(items, target, cap=100) == len(solutions)


def test_subset_search_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    for search in (matcher.dp_find_solution, matcher.dp_count_solutions):
        clock = iter([0.0, 2.0])
        monkeypatch.setattr(matcher, "time", SimpleNamespace(monotonic=clock.__next__))
        assert search([_eob(1, "synthetic")], 1, time_limit=1.0) is None


def test_exact_cascade_and_dp_respect_prior_claims_and_processing_date() -> None:
    payments = [_payment(cents, str(cents)) for cents in (3, 5, 6, 7, 1)]
    items = [
        _eob(3, "a"),
        _eob(3, "b"),
        _eob(5, "a", "c"),
        _eob(4, "d", processed=PAYMENT_DATE),
        _eob(2, "e"),
        _eob(6, "future", processed=datetime(2001, 1, 3)),
        _eob(2, "future-small", processed=datetime(2001, 1, 3)),
    ]
    results = matcher.match_payments(payments, items, [])

    assert [r.payment for r in results] == payments
    for result, claims, confidence in zip(
        results[:3],
        [["b"], ["a", "c"], ["e", "d"]],
        [MatchConfidence.EXACT, MatchConfidence.EXACT, MatchConfidence.DP_UNIQUE],
        strict=True,
    ):
        assert isinstance(result.result, Matched)
        assert result.result.confidence == confidence
        assert [cn for item in result.result.items for cn in item.claim_nums] == claims
    for result in results[3:]:
        assert result.result == NotMatched(reason=NotMatchedReason.NO_CANDIDATES, candidate_count=0)


@pytest.mark.parametrize(
    ("amounts", "reason", "count"),
    [((2, 2), NotMatchedReason.NO_SUBSET, 0), ((1, 2, 2), NotMatchedReason.AMBIGUOUS, 2)],
)
def test_unresolved_reports_only_remaining_subsets(
    amounts: tuple[int, ...], reason: NotMatchedReason, count: int
) -> None:
    items = [_eob(cents, str(i)) for i, cents in enumerate(amounts)] + [_eob(4, "used"), _eob(1, "used")]
    resolved, pending = matcher.match_payments([_payment(4, "first"), _payment(3, "second")], items, [])
    assert isinstance(resolved.result, Matched)
    assert pending.result == NotMatched(reason=reason, candidate_count=len(amounts), solution_count=count)


if __name__ == "__main__":
    pytest_bazel.main()
