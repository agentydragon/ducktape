"""Annual target weights, a cash band, and optional sleeve trade proposals."""

from finance.augur.policy import sleeves
from finance.augur.policy.cash_band import Invest, Raise, cash_band
from finance.augur.sim.actions import Action, DecisionActions, PayClaim
from finance.augur.sim.observations import Decision, Observation


def propose_trades(observation: Observation, *, annual_step: int, cash_reserve: int) -> list[Action]:
    """Compose one trade proposal, reserving caller-chosen claims and consumption."""
    if not 0 <= annual_step <= 10:
        raise ValueError("annual step must be in [0, 10] percentage points")
    if cash_reserve < 0:
        raise ValueError("cash reserve must be nonnegative")
    growth = 50 + annual_step * min(observation.month // 12, 4)
    targets = {("checking", "test-growth"): growth, ("checking", "test-steady"): 100 - growth}
    cash = dict(observation.accounts)["checking"]
    adjustment = cash_band(projected_cash=cash - cash_reserve, floor=0, ceiling=1_000_000)
    cause_id = f"allocation-m{observation.month}"
    if isinstance(adjustment, Raise):
        return sleeves.withdraw(
            observation, targets=targets, cash_account_id="checking", amount=adjustment.amount, cause_id=cause_id
        )
    if isinstance(adjustment, Invest):
        return sleeves.deposit(
            observation, targets=targets, cash_account_id="checking", cash_budget=adjustment.amount, cause_id=cause_id
        )
    return sleeves.rebalance(
        observation,
        targets=targets,
        cash_account_id="checking",
        cash_budget=cash - cash_reserve,
        tolerance_ppb=0,
        cause_id=cause_id,
    )


def decide(batch: list[Decision], *, annual_step: int) -> list[DecisionActions]:
    responses = []
    for decision in batch:
        observation = decision.observation
        cash = dict(observation.accounts)["checking"]
        due = sum(claim.amount_due for claim in observation.claims)
        trades = propose_trades(observation, annual_step=annual_step, cash_reserve=due)
        payments: list[Action] = [
            PayClaim(
                request_id=index,
                cause_id=f"pay-{claim.cause_id}",
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
        ]
        actions = trades + payments if cash < due else payments + trades
        responses.append(DecisionActions(rollout_id=decision.rollout_id, month=observation.month, actions=actions))
    return responses
