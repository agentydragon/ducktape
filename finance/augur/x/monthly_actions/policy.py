"""An authored batch rule: invest opening cash, sell whole lots to cover bills, then pay.

The example has one household cash account. Opening cash buys fractional shares
within the explicit budget; execution owns prices, basis and taxes.
"""

from finance.augur.rust.simulator import Action, Decision, DecisionActions
from finance.augur.sim.cash_band import Invest, Raise, cash_band
from finance.augur.sim.fixed_point import quantity_for_value


def decide(batch: list[Decision]) -> list[DecisionActions]:
    responses = []
    for decision in batch:
        observation = decision.observation
        positions = observation.public_positions
        claims = observation.claims
        adjustment = cash_band(
            projected_cash=observation.cash - sum(claim.amount_due for claim in claims), floor=0, ceiling=0
        )
        actions = []
        if isinstance(adjustment, Invest) and observation.month == 0 and not positions:
            pool = observation.holding_pools[0]
            units = quantity_for_value(adjustment.amount, pool.price, pool.quantity_scale, round_up=False)
            if units:
                actions.append(
                    Action.buy(
                        cause_id="opening-investment",
                        from_account=(observation.agent_id, "checking"),
                        holding_account_id=pool.account_id,
                        asset_id=pool.asset_id,
                        lot_id="opening-investment",
                        units=units,
                        quantity_scale=pool.quantity_scale,
                    )
                )
        # This rule liquidates all lots, not exactly the proposed raise. Later
        # investment proposals are ignored, leaving any surplus cash available.
        if isinstance(adjustment, Raise):
            actions.extend(
                Action.sell(
                    cause_id=f"fund-{position.lot_id}",
                    agent_id=observation.agent_id,
                    proceeds_account_id="checking",
                    asset_id=position.asset_id,
                    lots=[(position.account_id, position.lot_id, position.units)],
                )
                for position in positions
            )
        actions.extend(
            Action.pay_claim(
                request_id=request_id,
                cause_id=f"pay-{claim.cause_id}",
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for request_id, claim in enumerate(claims)
        )
        responses.append(DecisionActions(rollout_id=decision.rollout_id, month=observation.month, actions=actions))
    return responses
