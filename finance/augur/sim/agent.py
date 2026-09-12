"""An economic actor whose state lives on the instance; the World invokes it once per month."""

from abc import ABC, abstractmethod

from finance.augur.sim.actions import Action
from finance.augur.sim.observations import Observation


class EconomicAgent(ABC):
    """Subclass with the experiment's real state (spending tier, memory, parameters).

    `decide` handles the month-opened message: it sees the actor's current facts and
    inbox and returns the ordered actions the actor requests. A rejected action stops
    the path and no later `decide` call follows on it.
    """

    def __init__(self, agent_id: str) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id must not be empty")
        self.agent_id = agent_id

    @abstractmethod
    def decide(self, observation: Observation) -> list[Action]: ...
