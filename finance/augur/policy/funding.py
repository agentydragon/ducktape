"""Optional sales-only funding of claims from one cash account; surplus cash stays idle."""

from finance.augur.policy.sleeves import withdraw
from finance.augur.sim.actions import DecisionActions, PayClaim
from finance.augur.sim.observations import Decision


def fund_claims(
    batch: list[Decision], *, targets: dict[tuple[str, str], int], cash_account_id: str
) -> list[DecisionActions]:
    """Fund claims on the chosen cash account, then propose full payments in observed order.

    The caller supplies claims payable from this account and selected holding-pool weights.
    Sales are overweight-first/FIFO; neither purchases nor tax gross-up are proposed.
    """
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
            PayClaim(
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
