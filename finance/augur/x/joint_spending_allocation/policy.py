"""One household composing annual bounded spending with cash-band/glide allocation."""

from dataclasses import dataclass

from finance.augur.sim.actions import Action, Consume, PayClaim
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.books import AccountRef
from finance.augur.sim.observations import Observation
from finance.augur.x.allocation_glide.policy import propose_trades
from finance.augur.x.bounded_spending import python_policy
from finance.augur.x.bounded_spending.python_policy import Parameters, ScalarPolicy


@dataclass(frozen=True)
class Intention:
    consumption: int
    fixed_real_anchor: int


class JointHousehold(EconomicAgent):
    """One path's spending memory and recorded intentions live on the instance."""

    def __init__(self, parameters: Parameters, *, annual_step: int) -> None:
        super().__init__("retiree")
        self.rule = ScalarPolicy(parameters)
        self.reference = ScalarPolicy(Parameters(parameters.rate_bps, 0, 0))
        self.annual_step = annual_step
        self.intentions: dict[int, Intention] = {}

    def decide(self, observation: Observation) -> list[Action]:
        if observation.cpi is None:
            raise ValueError("bounded spending requires a supplied CPI path")
        view = python_policy.Observation(
            observation.month, observation.cash, observation.public_holdings, observation.cpi[0]
        )
        amount, anchor = self.rule(view), self.reference(view)
        self.intentions[observation.month] = Intention(amount, anchor)
        claims = observation.claims
        actions = propose_trades(
            observation, annual_step=self.annual_step, cash_reserve=amount + sum(claim.amount_due for claim in claims)
        )
        actions.extend(
            PayClaim(
                request_id=index,
                cause_id=f"pay-{claim.cause_id}",
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(claims)
        )
        if amount:
            actions.append(
                Consume(
                    request_id=len(claims),
                    cause_id=f"annual_consumption_m{observation.month}",
                    component_id="annual_consumption",
                    from_account=AccountRef(agent_id=observation.agent_id, account_id="checking"),
                    to_account=AccountRef(agent_id="world", account_id="checking"),
                    amount=amount,
                )
            )
        return actions
