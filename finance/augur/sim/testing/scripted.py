"""Test households that submit scripted actions or fund claims by selling."""

from collections.abc import Mapping, Sequence

from finance.augur.model.series import SecurityKey
from finance.augur.policy.cash_band_household import CashBandHousehold, SecuritySleeve
from finance.augur.sim.actions import Action
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.ids import AccountId, AgentId, AssetId
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


def sell_into_cash(asset: SecurityKey) -> CashBandHousehold:
    """An Alice household with no cash floor or ceiling that funds claims by selling."""
    return CashBandHousehold(
        AgentId("alice"),
        cash_account_id=AccountId("checking"),
        floor=0,
        ceiling=0,
        sleeves=(SecuritySleeve(asset_id=AssetId(asset.symbol), weight=1),),
        source_account_ids=(AccountId("checking"),),
        reinvest=None,
        cause_id_prefix="allocation_sale",
    )
