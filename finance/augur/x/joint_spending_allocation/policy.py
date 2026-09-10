"""Compose annual spending and allocation proposals in one monthly action response."""

from dataclasses import dataclass

from finance.augur.sim.actions import Consume, DecisionActions, PayClaim
from finance.augur.sim.books import AccountRef
from finance.augur.sim.observations import Decision
from finance.augur.x.allocation_glide.policy import propose_trades
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Observations, Parameters


@dataclass(frozen=True)
class Intention:
    consumption: int
    fixed_real_anchor: int


class JointPolicy:
    def __init__(self, parameters: Parameters, *, rollout_count: int, annual_step: int) -> None:
        self.rule = BatchPolicy(parameters, rollout_count)
        self.reference = BatchPolicy(Parameters(parameters.rate_bps, 0, 0), rollout_count)
        self.annual_step = annual_step
        self.intentions: dict[int, dict[int, Intention]] = {}

    def __call__(self, batch: list[Decision]) -> list[DecisionActions]:
        observations = Observations.from_native(batch)
        responses = []
        for decision, amount, anchor in zip(batch, self.rule(observations), self.reference(observations), strict=True):
            observation = decision.observation
            self.intentions.setdefault(decision.rollout_id, {})[observation.month] = Intention(amount, anchor)
            claims = observation.claims
            actions = propose_trades(
                observation,
                annual_step=self.annual_step,
                cash_reserve=amount + sum(claim.amount_due for claim in claims),
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
            responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
        return responses
