"""Full payment of due claims: alone, or funded by sales-only withdrawals from one cash account; surplus stays idle."""

from collections.abc import Sequence

from finance.augur.policy.sleeves import withdraw
from finance.augur.sim.actions import Action, DecisionActions, PayClaim
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.ids import AccountId, AssetId
from finance.augur.sim.observations import Claim, Decision, Observation


def full_payments(claims: Sequence[Claim]) -> list[PayClaim]:
    """A full payment of every claim, in observed order."""
    return [
        PayClaim(
            request_id=index + 1,
            cause_id=claim.cause_id,
            claim=claim,
            from_account=claim.from_account,
            amount=claim.amount_due,
        )
        for index, claim in enumerate(claims)
    ]


class ClaimPayer(EconomicAgent):
    """Pays every due claim in full, in observed order, and never trades: a claim cash cannot cover stops the path."""

    def decide(self, observation: Observation) -> list[Action]:
        return [*full_payments(observation.claims)]


def fund_claims(
    batch: list[Decision], *, targets: dict[tuple[AccountId, AssetId], int], cash_account_id: AccountId
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
        actions.extend(full_payments(claims))
        responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
    return responses
