"""Multi-pass payment matcher with confidence levels.

Treats matching as a constraint satisfaction problem: each payment must be
assigned a non-overlapping subset of items that sums to its amount. Exact
singles take precedence, then DP checks full and size-filtered candidate sets.
"""

import sys
import time
from dataclasses import dataclass, field

from x.eob_matching.models import (
    EOB,
    BankPayment,
    MatchConfidence,
    Matched,
    MatchedItem,
    NotMatched,
    NotMatchedReason,
    PaymentResult,
)

# Time limit per DP invocation in seconds.
DP_TIME_LIMIT_SECS = 5.0

# Restrict the final matching pass to items at least this large.
LARGE_ITEM_MIN_CENTS = 10_000  # $100


@dataclass
class MatchingState:
    """Global mutable state for the matching process."""

    all_items: list[EOB]
    payments: list[BankPayment]
    used_claims: set[str] = field(default_factory=set)
    results: dict[str, PaymentResult] = field(default_factory=dict)

    @property
    def unmatched_payments(self) -> list[BankPayment]:
        return [p for p in self.payments if p.payment_id not in self.results]

    def candidates_for(self, payment: BankPayment, *, min_cents: int = 1) -> list[EOB]:
        """Items eligible for this payment: unused, processed before payment, within amount."""
        return [
            item
            for item in self.all_items
            if not (set(item.claim_nums) & self.used_claims)
            and min_cents <= item.plan_paid_cents <= payment.cents
            and (item.latest_proc_date is None or item.latest_proc_date <= payment.date_dt)
        ]

    def commit(self, payment: BankPayment, items: list[EOB], confidence: MatchConfidence) -> None:
        """Record a match and mark all claim_nums as used."""
        for item in items:
            for cn in item.claim_nums:
                if cn in self.used_claims:
                    raise ValueError(f"Claim {cn} already used — double attribution")
                self.used_claims.add(cn)

        matched_items = [
            MatchedItem(
                claim_nums=item.claim_nums,
                provider=item.provider,
                plan_paid_total=item.plan_paid_total,
                is_pharmacy_orphan=(item.statement_date == ""),
            )
            for item in items
        ]

        self.results[payment.payment_id] = PaymentResult(
            payment=payment,
            result=Matched(
                items=matched_items, total_claims=sum(len(mi.claim_nums) for mi in matched_items), confidence=confidence
            ),
        )


def dp_find_solution(items: list[EOB], target_cents: int, time_limit: float = DP_TIME_LIMIT_SECS) -> list[EOB] | None:
    """Find one subset of items summing to target_cents via 0-1 knapsack DP.

    Returns None if no solution or time limit exceeded.
    """
    if not items:
        return None

    deadline = time.monotonic() + time_limit
    unreachable = -2
    dp = [unreachable] * (target_cents + 1)
    dp[0] = -1

    for i, item in enumerate(items):
        if time.monotonic() > deadline:
            return None  # bail out
        c = item.plan_paid_cents
        if c <= 0 or c > target_cents:
            continue
        for j in range(target_cents, c - 1, -1):
            if dp[j] == unreachable and dp[j - c] != unreachable:
                dp[j] = i

    if dp[target_cents] == unreachable:
        return None

    result: list[EOB] = []
    j = target_cents
    while j > 0:
        i = dp[j]
        result.append(items[i])
        j -= items[i].plan_paid_cents
    return result


def dp_count_solutions(
    items: list[EOB], target_cents: int, cap: int = 2, time_limit: float = DP_TIME_LIMIT_SECS
) -> int | None:
    """Count subsets summing to target_cents, stopping at cap.

    Returns None if time limit exceeded.
    """
    if not items:
        return 0

    deadline = time.monotonic() + time_limit
    dp = [0] * (target_cents + 1)
    dp[0] = 1

    for item in items:
        if time.monotonic() > deadline:
            return None  # bail out
        c = item.plan_paid_cents
        if c <= 0 or c > target_cents:
            continue
        for j in range(target_cents, c - 1, -1):
            dp[j] += dp[j - c]
            if dp[j] > cap:
                dp[j] = cap + 1

    return min(dp[target_cents], cap + 1)


def _pass_exact_singles(state: MatchingState) -> int:
    """Match payments where exactly one item has the right amount. Returns count matched."""
    matched = 0
    for payment in state.unmatched_payments:
        candidates = state.candidates_for(payment)
        exact_matches = [item for item in candidates if item.plan_paid_cents == payment.cents]
        if len(exact_matches) == 1:
            state.commit(payment, exact_matches, MatchConfidence.EXACT)
            matched += 1
            print(
                f"  [EXACT] {payment.date}  ${payment.amount:>12,.2f}  {payment.payment_id}  "
                f"{exact_matches[0].provider}",
                file=sys.stderr,
            )
    return matched


def _cascade_exact_singles(state: MatchingState) -> None:
    while _pass_exact_singles(state):
        pass


def _pass_dp_unique(state: MatchingState, label: str, *, min_cents: int = 1) -> int:
    """Match unique subsets within this pass's candidate set."""
    matched = 0
    for payment in state.unmatched_payments:
        candidates = state.candidates_for(payment, min_cents=min_cents)
        if not candidates:
            continue

        solution = dp_find_solution(candidates, payment.cents)
        if solution is None:
            continue

        if dp_count_solutions(candidates, payment.cents, cap=2) == 1:
            state.commit(payment, solution, MatchConfidence.DP_UNIQUE)
            matched += 1
            providers = {item.provider for item in solution}
            print(
                f"  [{label}] {payment.date}  ${payment.amount:>12,.2f}  {payment.payment_id}  "
                f"{len(solution)} items  {'; '.join(sorted(providers))}",
                file=sys.stderr,
            )
    return matched


def _finalize_unmatched(state: MatchingState) -> None:
    """Mark remaining payments as not matched with reasons."""
    for payment in state.unmatched_payments:
        candidates = state.candidates_for(payment)

        if not candidates:
            reason = NotMatchedReason.NO_CANDIDATES
            solution_count = None
        else:
            solution = dp_find_solution(candidates, payment.cents)
            if solution is None:
                # DP found nothing (or timed out)
                reason = NotMatchedReason.NO_SUBSET
                solution_count = 0
            else:
                count = dp_count_solutions(candidates, payment.cents, cap=100)
                reason = NotMatchedReason.AMBIGUOUS
                solution_count = count

        state.results[payment.payment_id] = PaymentResult(
            payment=payment,
            result=NotMatched(reason=reason, candidate_count=len(candidates), solution_count=solution_count),
        )
        print(
            f"  [{reason.value.upper()}] {payment.date}  ${payment.amount:>12,.2f}  "
            f"{payment.payment_id}  ({len(candidates)} candidates"
            f"{f', {solution_count} solutions' if solution_count is not None else ''})",
            file=sys.stderr,
        )


def match_payments(payments: list[BankPayment], eobs: list[EOB], pharmacy_orphans: list[EOB]) -> list[PaymentResult]:
    """Run the matching passes in priority order and report remaining payments."""
    state = MatchingState(all_items=eobs + pharmacy_orphans, payments=payments)

    # Pass 1: Exact single-item matches (with cascade)
    print("Pass 1: Exact single-item matches", file=sys.stderr)
    _cascade_exact_singles(state)

    # Pass 2: DP unique matches (with cascade back to pass 1)
    print("\nPass 2: DP unique matches", file=sys.stderr)
    while _pass_dp_unique(state, "DP_UNIQUE"):
        _cascade_exact_singles(state)

    # Pass 3: Large-item subsets
    print("\nPass 3: Large payments (filtered candidates)", file=sys.stderr)
    if _pass_dp_unique(state, "LARGE_UNIQUE", min_cents=LARGE_ITEM_MIN_CENTS):
        _cascade_exact_singles(state)

    # Pass 4: Finalize unmatched
    print("\nPass 4: Flagging unmatched", file=sys.stderr)
    _finalize_unmatched(state)

    # Return results in payment order
    return [state.results[p.payment_id] for p in payments]
