"""Sales-only funding of fixed indexed claims; coupons may accumulate in cash."""

from finance.augur.policy.sleeves import withdraw
from finance.augur.rust.simulator import Action, Decision, DecisionActions


def fund_claims(
    batch: list[Decision], *, targets: dict[tuple[str, str], int], cash_account_id: str
) -> list[DecisionActions]:
    """Propose overweight-first/FIFO sales, then full claim payments in observed order."""
    responses = []
    for decision in batch:
        observation = decision.observation
        claims = observation.claims
        cash = dict(observation.accounts)[cash_account_id]
        actions = withdraw(
            observation,
            targets=targets,
            cash_account_id=cash_account_id,
            amount=max(0, sum(claim.amount_due for claim in claims) - cash),
            cause_id=f"withdrawal-funding-{observation.month}",
        )
        actions.extend(
            Action.pay_claim(
                request_id=index + 1,
                cause_id=claim.cause_id,
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(claims)
        )
        responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
    return responses
