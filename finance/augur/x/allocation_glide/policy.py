"""Annual target weights, a cash band, and optional sleeve trade proposals."""

from finance.augur.rust.simulator import Action, Decision, DecisionActions
from finance.augur.sim import sleeves
from finance.augur.sim.cash_band import Invest, Raise, cash_band


def decide(batch: list[Decision], *, annual_step: int) -> list[DecisionActions]:
    if not 0 <= annual_step <= 10:
        raise ValueError("annual step must be in [0, 10] percentage points")
    responses = []
    for decision in batch:
        observation = decision.observation
        growth = 50 + annual_step * min(observation.month // 12, 4)
        targets = {("checking", "test-growth"): growth, ("checking", "test-steady"): 100 - growth}
        cash = dict(observation.accounts)["checking"]
        due = sum(claim.amount_due for claim in observation.claims)
        adjustment = cash_band(projected_cash=cash - due, floor=0, ceiling=1_000_000)
        cause_id = f"allocation-m{observation.month}"
        if isinstance(adjustment, Raise):
            trades = sleeves.withdraw(
                observation, targets=targets, cash_account_id="checking", amount=adjustment.amount, cause_id=cause_id
            )
        elif isinstance(adjustment, Invest):
            trades = sleeves.deposit(
                observation,
                targets=targets,
                cash_account_id="checking",
                cash_budget=adjustment.amount,
                cause_id=cause_id,
            )
        else:
            trades = sleeves.rebalance(
                observation,
                targets=targets,
                cash_account_id="checking",
                cash_budget=cash - due,
                tolerance_ppb=0,
                cause_id=cause_id,
            )
        payments = [
            Action.pay_claim(
                request_id=index,
                cause_id=f"pay-{claim.cause_id}",
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
        ]
        actions = trades + payments if isinstance(adjustment, Raise) else payments + trades
        responses.append(DecisionActions(rollout_id=decision.rollout_id, month=observation.month, actions=actions))
    return responses
