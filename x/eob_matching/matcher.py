"""Multi-pass payment matcher with confidence levels.

Treats matching as a constraint satisfaction problem: each payment must be
assigned a non-overlapping subset of items that sums to its amount. Only
commits matches that are provably unique.
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

# For large payments above DP cap, filter to items above this threshold
# to reduce candidate count enough for DP.
LARGE_PAYMENT_MIN_ITEM_CENTS = 10_000  # $100


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

    def candidates_for(self, payment: BankPayment) -> list[EOB]:
        """Items eligible for this payment: unused, processed before payment, within amount."""
        return [
            item
            for item in self.all_items
            if not (set(item.claim_nums) & self.used_claims)
            and item.plan_paid_cents > 0
            and item.plan_paid_cents <= payment.cents
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


def _pass_dp_unique(state: MatchingState) -> int:
    """Match payments where DP finds exactly one valid subset. Returns count matched."""
    matched = 0
    for payment in state.unmatched_payments:
        candidates = state.candidates_for(payment)
        if not candidates:
            continue

        solution = dp_find_solution(candidates, payment.cents)
        if solution is None:
            continue

        count = dp_count_solutions(candidates, payment.cents, cap=2)
        if count is not None and count == 1:
            state.commit(payment, solution, MatchConfidence.DP_UNIQUE)
            matched += 1
            providers = {item.provider for item in solution}
            print(
                f"  [DP_UNIQUE] {payment.date}  ${payment.amount:>12,.2f}  {payment.payment_id}  "
                f"{len(solution)} items  {'; '.join(sorted(providers))}",
                file=sys.stderr,
            )
    return matched


def _pass_large_payments(state: MatchingState) -> int:
    """Handle payments above DP cap by filtering to large items only."""
    matched = 0
    for payment in state.unmatched_payments:
        candidates = state.candidates_for(payment)
        # Filter to large items to make DP feasible
        large_candidates = [c for c in candidates if c.plan_paid_cents >= LARGE_PAYMENT_MIN_ITEM_CENTS]
        if not large_candidates:
            continue
        solution = dp_find_solution(large_candidates, payment.cents)
        if solution is None:
            continue

        count = dp_count_solutions(large_candidates, payment.cents, cap=2)
        if count is not None and count == 1:
            state.commit(payment, solution, MatchConfidence.DP_UNIQUE)
            matched += 1
            providers = {item.provider for item in solution}
            print(
                f"  [LARGE_UNIQUE] {payment.date}  ${payment.amount:>12,.2f}  {payment.payment_id}  "
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
    """Multi-pass matcher. Only commits provably unique matches."""
    state = MatchingState(all_items=eobs + pharmacy_orphans, payments=payments)

    # Pass 1: Exact single-item matches (with cascade)
    print("Pass 1: Exact single-item matches", file=sys.stderr)
    while _pass_exact_singles(state) > 0:
        pass  # cascade until no more found

    # Pass 2: DP unique matches (with cascade back to pass 1)
    print("\nPass 2: DP unique matches", file=sys.stderr)
    changed = True
    while changed:
        changed = _pass_dp_unique(state) > 0
        if changed:
            # Cascade: new unique singles may have appeared
            while _pass_exact_singles(state) > 0:
                pass

    # Pass 3: Large payments
    print("\nPass 3: Large payments (filtered candidates)", file=sys.stderr)
    if _pass_large_payments(state) > 0:
        # Cascade again
        while _pass_exact_singles(state) > 0:
            pass

    # Pass 4: Finalize unmatched
    print("\nPass 4: Flagging unmatched", file=sys.stderr)
    _finalize_unmatched(state)

    # Return results in payment order
    return [state.results[p.payment_id] for p in payments]
