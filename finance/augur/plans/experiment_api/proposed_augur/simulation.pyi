"""Execute financial timelines from supplied worlds and actor-owned policies.

Coordinates observations, funding, transactions, contractual payments and taxes
through shared financial mechanics. Owns scheduling, acceptance/rejection and
isolated per-path state, not policy selection, market fitting or experiment sweeps.
simulate starts fresh; resume clones complete checkpoints and only initializes
replacement policy components. Neither mutates reusable input worlds or situations.

Same-time ordering must be explicit; an atomic purchase cannot leave half a loan
on one actor's book. Failed optional actions do not erase settled obligations.
Pre-sampled markets assume these actors do not move external prices. Multi-agent
books are supported without requiring a general equilibrium model.
"""

from collections.abc import Mapping
from typing import Literal

from proposed_augur.accounting import Actor
from proposed_augur.markets import ContinuationWorlds, Worlds
from proposed_augur.policies import Strategy
from proposed_augur.results import ContinuationRun, Observer, Run
from proposed_augur.state import CheckpointBatch, Situation

class AnnualConvention:
    name: str
    @classmethod
    def withdraw_then_return(cls) -> AnnualConvention: ...
    @classmethod
    def withdraw_then_return_then_rebalance(cls) -> AnnualConvention: ...

def simulate(
    situation: Situation,
    *,
    policies: Mapping[Actor, Strategy],
    reporting_actor: Actor,
    worlds: Worlds,
    observers: Mapping[str, Observer],
    on_shortfall: Literal["stop"],
    convention: AnnualConvention | None = None,
) -> Run: ...
def resume(
    checkpoints: CheckpointBatch,
    *,
    replace_policies: Mapping[Actor, Strategy],
    worlds: ContinuationWorlds,
    observers: Mapping[str, Observer],
    on_shortfall: Literal["stop"],
    reporting_actor: Actor,
) -> ContinuationRun: ...
