"""A household that submits a test's explicit actions at their months, ahead of another household's own."""

from collections.abc import Mapping, Sequence

from finance.augur.sim.actions import Action
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.observations import Observation


class Scripted(EconomicAgent):
    """Each month's scripted actions first, then what `then` decides on the same observation.

    `then` never sees the scripted actions, so anything it sizes from projected cash ignores
    their proceeds.
    """

    def __init__(self, then: EconomicAgent, script: Mapping[int, Sequence[Action]]) -> None:
        super().__init__(then.agent_id)
        self.then = then
        self.script = script

    def decide(self, observation: Observation) -> list[Action]:
        return [*self.script.get(observation.month, ()), *self.then.decide(observation)]
